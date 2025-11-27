from contextlib import asynccontextmanager
from .constants import MCPMode
from contextvars import ContextVar
from fastapi import APIRouter, Request
from fastapi.dependencies.utils import get_dependant, get_flat_dependant, _should_embed_body_fields
from fastapi.middleware.asyncexitstack import AsyncExitStackMiddleware
import httpx
from mcp.server.fastmcp import FastMCP
from starlette.routing import Route
from starlette.responses import Response
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple
import inspect
import logging

logger = logging.getLogger(__name__)

request_context: ContextVar[Optional[Dict[str, str]]] = ContextVar("request_context", default=None)

class HeaderCaptureMiddleware:
    """
    ASGI Middleware to capture headers from the incoming MCP request 
    and store them in a ContextVar for the tool execution to use.
    
    Implemented as pure ASGI to support SSE streaming correctly.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Capture headers from scope
        # ASGI headers are list of [bytes, bytes]
        headers_list = scope.get("headers", [])
        decoded_headers = {}
        for k, v in headers_list:
            try:
                decoded_headers[k.decode("latin-1")] = v.decode("latin-1")
            except Exception:
                pass
        
        token = request_context.set(decoded_headers)
        try:
            await self.app(scope, receive, send)
        finally:
            request_context.reset(token)

class AppProxyResponse(Response):
    """
    Helper response class to execute a sub-application (ASGI app) 
    within the context of a handler, preserving the modified scope.
    """
    def __init__(self, app):
        super().__init__()
        self.app = app
    
    async def __call__(self, scope, receive, send):
        # Execute the sub-app with the scope provided by Starlette/FastAPI
        # This scope will have been modified by the proxy handler
        await self.app(scope, receive, send)

class MCPIt:
    def __init__(self, name: str, version: str = "1.0.0", json_response: bool = True):
        self.name = name
        self.version = version
        self.json_response = json_response
        self._registry: List[Dict[str, Any]] = []
        
        self.fastmcp = FastMCP(name, json_response=json_response, streamable_http_path="/")

    def mcp(self, mode: Literal['tool', 'resource', 'prompt'] = 'tool', **kwargs):
        """
        Decorator to register a FastAPI route as an MCP capability.
        
        Args:
            mode: The type of MCP capability ('tool', 'resource', 'prompt').
            **kwargs: Additional arguments for the specific capability (e.g., name, description).
        """
        def decorator(func: Callable):
            self._registry.append({
                "func": func,
                "mode": mode,
                "kwargs": kwargs
            })
            return func
        return decorator

    def _find_route_for_func(self, router: APIRouter, func: Callable) -> Optional[Route]:
        """Find the FastAPI route corresponding to the decorated function."""
        for route in router.routes:
            # Check if it's a generic Route (Starlette/FastAPI)
            if hasattr(route, "endpoint") and route.endpoint == func:
                return route
        return None

    def _get_route_params_structure(self, route: Route) -> Tuple[Dict[str, Literal['query', 'body', 'path']], Optional[str], List[Any]]:
        """
        Analyze a FastAPI route to determine where each parameter belongs.
        Returns:
             - A dict: {param_name: 'query' | 'body' | 'path'}
             - The name of the single body parameter if one exists and should be flattened (not embedded), else None.
             - A list of flattened, relevant parameters (ModelField objects).
        """
        try:
            dependant = get_dependant(path=route.path, call=route.endpoint)
            flat_dependant = get_flat_dependant(dependant)
            
            param_map = {}
            single_body_param = None
            relevant_params = []
            
            for param in flat_dependant.path_params:
                param_map[param.name] = 'path'
                relevant_params.append(param)
                
            for param in flat_dependant.query_params:
                param_map[param.name] = 'query'
                relevant_params.append(param)
                
            if flat_dependant.body_params:
                # Not cool to use the internal function, but better than reimplementing everything and risk missing further changes.
                if not _should_embed_body_fields(flat_dependant.body_params):
                    single_body_param = flat_dependant.body_params[0].name

                for param in flat_dependant.body_params:
                    param_map[param.name] = 'body'
                    relevant_params.append(param)
            
            return param_map, single_body_param, relevant_params
        except Exception as e:
            logger.warning(f"Failed to analyze route params for {route.path}: {e}")
            return {}, None, []

    async def _internal_proxy_call(
        self, 
        router: APIRouter, 
        path: str, 
        method: str, 
        params: Dict[str, Any], 
        param_structure: Dict[str, str],
        single_body_param: Optional[str] = None
    ) -> Any:
        """
        Make an in-process call to the FastAPI app using ASGITransport.
        Splits params into query/body based on route definition.
        """
        transport = httpx.ASGITransport(app=AsyncExitStackMiddleware(router))
        base_url = "http://mcp-internal"
        
        # Retrieve headers from context
        headers = request_context.get() or {}
        
        filtered_headers = {
            k: v for k, v in headers.items() 
            if k.lower() not in ('host', 'content-length', 'content-type', 'connection', 'upgrade')
        }
        
        filtered_headers['X-MCP-Source'] = 'true'

        # Split params based on structure
        query_params = {}
        body_params = {}
        path_params = {}

        for key, value in params.items():
            location = param_structure.get(key, None)
            
            if location == 'path':
                path_params[key] = value
            elif location == 'query':
                query_params[key] = value
            elif location == 'body':
                body_params[key] = value
            else:
                # Default fallback logic if analysis failed or param is new
                if method.upper() in ["GET", "DELETE"]:
                    query_params[key] = value
                else:
                    body_params[key] = value

        # Replace path parameters in the URL
        formatted_path = path
        for key, value in path_params.items():
            formatted_path = formatted_path.replace(f"{{{key}}}", str(value))

        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            req_kwargs = {}
            if query_params:
                req_kwargs["params"] = query_params
            if body_params:
                # If we identified a single body param that shouldn't be embedded/wrapped
                if single_body_param and len(body_params) == 1 and single_body_param in body_params:
                     val = body_params[single_body_param]
                     # If it's a Pydantic model, dump it. If it's a dict or primitive, use as is.
                     req_kwargs["json"] = val.model_dump() if hasattr(val, "model_dump") else val
                else:
                     req_kwargs["json"] = {key: value.model_dump() if hasattr(value, "model_dump") else value for key, value in body_params.items()}

            try:
                response = await client.request(
                    method=method,
                    url=formatted_path,
                    headers=filtered_headers,
                    **req_kwargs
                )
                
                if self.json_response:
                    try:
                        return response.json()
                    except Exception:
                        return response.text
                return response.text
            except Exception as e:
                logger.error(f"Error in MCP internal call to {path}: {e}")
                raise e

    def build(self, router: APIRouter, transport: Literal['sse', 'streamable-http'] = 'streamable-http', mount_path: str = "/mcp"):
        """
        Build the MCP server by registering tools and mounting routes.
        """
        for item in self._registry:
            # 1. Process Registry and Register Tools on FastMCP
            func, mode, kwargs = item['func'], item['mode'], item['kwargs']
            
            route = self._find_route_for_func(router, func)
            if not route:
                logger.warning(f"Function {func.__name__} is decorated with @mcp but not registered as a FastAPI route. Skipping.")
                continue

            # Analyze parameter structure once during build time
            param_structure, single_body_param, relevant_params = self._get_route_params_structure(route)

            #TODO: Add support for resource and prompt modes.
            if mode == MCPMode.TOOL:
                tool_name = kwargs.get('name', func.__name__)
                tool_desc = kwargs.get('description', func.__doc__ or "")
                
                # 1. Construct parameters from relevant_params (ModelFields)
                new_params = []
                for field in relevant_params:
                    # Determine default value
                    default = inspect.Parameter.empty
                    if not field.required:
                        default = field.default
                    
                    param = inspect.Parameter(
                        name=field.name,
                        kind=inspect.Parameter.KEYWORD_ONLY,
                        default=default,
                        annotation=field.type_
                    )
                    new_params.append(param)
                
                # Define the wrapper
                def create_wrapper(current_router, current_route, current_tool_name, current_structure, current_single_body_param):
                    async def wrapper(**call_params):
                        import logging
                        _logger = logging.getLogger(__name__)
                        
                        # Internally call the actual FastAPI route using ASGITransport.
                        return await self._internal_proxy_call(
                            router=current_router,
                            path=current_route.path,
                            method=list(current_route.methods)[0] if current_route.methods else "GET",
                            params=call_params,
                            param_structure=current_structure,
                            single_body_param=current_single_body_param
                        )
                    return wrapper

                wrapper = create_wrapper(router, route, tool_name, param_structure, single_body_param)
                
                # Apply metadata, signature and annotations
                wrapper.__doc__ = func.__doc__
                wrapper.__name__ = func.__name__
                wrapper.__signature__ = inspect.Signature(parameters=new_params)
                
                # CRITICAL: FastMCP introspection might ignore __signature__ if __annotations__ are missing
                wrapper.__annotations__ = {p.name: p.annotation for p in new_params}

                # Register with FastMCP
                self.fastmcp.tool(name=tool_name, description=tool_desc)(wrapper)

        # 2. Get the SSE App from FastMCP
        mcp_app = self.fastmcp.streamable_http_app() if transport == 'streamable-http' else self.fastmcp.sse_app()
        
        # 3. Add our Header Capture Middleware
        mcp_app.add_middleware(HeaderCaptureMiddleware)
        
        # 4. Mount it to the main FastAPI app
        async def proxy_handler(request: Request):
            # Mutate the scope to simulate a Mount
            # We strip the mount_path from the path, and append it to the root_path
            path_suffix = request.path_params.get("path", "")
        
            request.scope['path'] = "/" + path_suffix
            request.scope['root_path'] = request.scope.get('root_path', '') + mount_path
            
            return AppProxyResponse(mcp_app)

        router.add_api_route(mount_path, proxy_handler, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
        
        if transport == 'streamable-http':
            # Clearly a hack. I'm not sure yet if this is the best solution, but seems to be the most feasible for now.
            original_lifespan = router.lifespan_context
            @asynccontextmanager
            async def wrapped_lifespan(app):
                async with self.fastmcp._session_manager.run():
                    # The router lifespan usually takes the app as an argument.
                    if original_lifespan:
                         async with original_lifespan(app) as state:
                             yield state
                    else:
                        yield {}
            router.lifespan_context = wrapped_lifespan