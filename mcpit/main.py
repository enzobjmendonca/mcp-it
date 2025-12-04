from contextlib import asynccontextmanager
from .constants import MCPMode
from contextvars import ContextVar
from fastapi import APIRouter, Request
from fastapi.dependencies.utils import get_dependant, get_flat_dependant, _should_embed_body_fields
from fastapi.middleware.asyncexitstack import AsyncExitStackMiddleware
import httpx
from pydantic import create_model
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
    def __init__(self, name: str, json_response: bool = True):
        self.name = name
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
                "type": "local",
                "func": func,
                "mode": mode,
                "kwargs": kwargs
            })
            return func
        return decorator

    def proxy(self, url: str, method: str = "GET", mode: Literal['tool', 'resource', 'prompt'] = 'tool', **kwargs):
        """
        Decorator to register an external API endpoint as an MCP capability.
        The decorated function's signature is used to define the tool's interface,
        but the implementation is replaced by a proxy call to the specified URL.
        """
        def decorator(func: Callable):
            self._registry.append({
                "type": "proxy",
                "func": func,
                "mode": mode,
                "url": url,
                "method": method,
                "kwargs": kwargs
            })
            return func
        return decorator

    def _parse_openapi_schema(self, schema: Dict[str, Any], name: str) -> Any:
        """
        Convert a simple OpenAPI schema to a Python type or Pydantic model.
        """
        schema_type = schema.get("type", "object")
        
        if schema_type == "string":
            return str
        elif schema_type == "integer":
            return int
        elif schema_type == "number":
            return float
        elif schema_type == "boolean":
            return bool
        elif schema_type == "array":
            item_type = self._parse_openapi_schema(schema.get("items", {}), f"{name}Item")
            return List[item_type]
        elif schema_type == "object":
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            
            fields = {}
            for prop_name, prop_schema in properties.items():
                prop_type = self._parse_openapi_schema(prop_schema, f"{name}_{prop_name}")
                is_required = prop_name in required
                
                if is_required:
                    fields[prop_name] = (prop_type, ...)
                else:
                    fields[prop_name] = (Optional[prop_type], None)
            
            if not fields:
                return Dict[str, Any]
                
            return create_model(name, **fields)
        
        return Any

    def bind_openapi(
        self, 
        openapi_url: str,
        base_url: str = None,
        include_paths: Optional[List[str]] = None, 
        exclude_paths: Optional[List[str]] = None,
        name_from_summary: bool = False
    ):
        """
        Dynamically register tools from an OpenAPI specification.

        Args:
            openapi_url: The URL of the OpenAPI specification.
            base_url: The base URL of the server. If not provided, the base URL  from the openapi specification will be used.
            include_paths: The paths to include in the MCP server. If not provided, all paths will be included.
            exclude_paths: The paths to exclude in the MCP server. If not provided, no paths will be excluded.
            name_from_summary: If True, the name of the tool will be the summary of the operation. If False, the name will be the operationId.
        """
        try:
            resp = httpx.get(openapi_url)
            resp.raise_for_status()
            spec = resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch OpenAPI spec from {openapi_url}: {e}")
            return
        
        if not base_url:
            base_url = "/".join(openapi_url.split("/")[:-1])

        paths = spec.get("paths", {})
        
        for path, methods in paths.items():
            # Simple filtering
            if include_paths and not any(p in path for p in include_paths):
                continue
            if exclude_paths and any(p in path for p in exclude_paths):
                continue

            for method, op in methods.items():
                if method.lower() not in ["get", "post", "put", "delete", "patch"]:
                    continue
                
                op_id = op.get("operationId")
                name = op.get("summary").lower().replace(" ", "_") if name_from_summary else op_id

                description = op.get("description") or op.get("summary") or ""
                
                # Build parameters
                func_params = []
                param_structure = {}
                
                # 1. Path/Query params
                for param in op.get("parameters", []):
                    p_name = param.get("name")
                    p_required = param.get("required", False)
                    p_schema = param.get("schema", {})
                    p_type = self._parse_openapi_schema(p_schema, f"{op_id}_{p_name}")
                    p_in = param.get("in")
                    
                    if p_in in ["query", "path", "header", "cookie"]:
                        param_structure[p_name] = p_in

                    default = inspect.Parameter.empty if p_required else None
                    
                    func_params.append(
                        inspect.Parameter(
                            name=p_name,
                            kind=inspect.Parameter.KEYWORD_ONLY,
                            default=default,
                            annotation=p_type
                        )
                    )

                # 2. Request Body
                request_body = op.get("requestBody", {})
                content = request_body.get("content", {})
                json_media = content.get("application/json", {})
                
                if json_media:
                    schema = json_media.get("schema", {})
                    
                    # Let's try to flatten top-level properties if possible
                    if schema.get("type") == "object" and "properties" in schema:
                        body_model = self._parse_openapi_schema(schema, f"{op_id}Body")
                        # Extract fields from the generated model
                        for name, field_info in body_model.model_fields.items():
                            param_structure[name] = "body"
                            # Pydantic v2
                            annotation = field_info.annotation
                            default = field_info.default 
                            # Check for PydanticUndefined or similar
                            if field_info.is_required():
                                default = inspect.Parameter.empty
                            
                            func_params.append(
                                inspect.Parameter(
                                    name=name,
                                    kind=inspect.Parameter.KEYWORD_ONLY,
                                    default=default,
                                    annotation=annotation
                                )
                            )
                    else:
                        # Complex body or array, just pass as dict/Any called 'body'
                        param_structure["body"] = "body"
                        func_params.append(
                            inspect.Parameter(
                                name="body",
                                kind=inspect.Parameter.KEYWORD_ONLY,
                                default=inspect.Parameter.empty,
                                annotation=Dict[str, Any]
                            )
                        )
                
                # Create the dummy function signature
                sig = inspect.Signature(parameters=func_params)
                
                async def dummy_func(**kwargs): 
                    pass
                    
                dummy_func.__name__ = name
                dummy_func.__doc__ = description
                dummy_func.__signature__ = sig
                dummy_func.__annotations__ = {p.name: p.annotation for p in func_params}
                
                # Register
                self.proxy(
                    url=f"{base_url}{path}",
                    method=method.upper(),
                    name=name,
                    description=description,
                    param_structure=param_structure
                )(dummy_func)

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
        # Replace path parameters in the URL
        formatted_path = path
        for key, value in params.items():
            if param_structure.get(key) == 'path':
                formatted_path = formatted_path.replace(f"{{{key}}}", str(value))
        
        # Handle empty path routes: map "" to "/" for ASGITransport compatibility
        # When route has path="" (regex ^$), ASGI requires "/" but route expects ""
        # Solution: Create a temporary router with "/" route mapped to the same endpoint
        mapped_router = router
        
        if not formatted_path or formatted_path == "":
            # Check if any route in the router expects empty path
            for route in router.routes:
                if (hasattr(route, 'methods') and method.upper() in route.methods and 
                    hasattr(route, 'path') and route.path == "" and
                    hasattr(route, 'path_regex') and route.path_regex.pattern == "^$"):
                    # Found an empty path route - create a mapped router
                    mapped_router = APIRouter()
                    # Copy all routes from original router
                    for r in router.routes:
                        mapped_router.routes.append(r)
                    # Add a "/" route that points to the same endpoint
                    mapped_router.add_api_route(
                        "/",
                        route.endpoint,
                        methods=list(route.methods),
                        name=getattr(route, 'name', None)
                    )
                    formatted_path = "/"  # Use "/" for the call
                    break
        
        transport = httpx.ASGITransport(app=AsyncExitStackMiddleware(mapped_router))
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

    async def _external_proxy_call(self, url: str, method: str, params: Dict[str, Any], param_structure: Dict[str, str] = None) -> Any:
        """
        Make an external HTTP call to the proxied API.
        """
        # Retrieve headers from context
        headers = request_context.get() or {}
        filtered_headers = {
            k: v for k, v in headers.items() 
            if k.lower() not in ('host', 'content-length', 'content-type', 'connection', 'upgrade')
        }
        filtered_headers['X-MCP-Source'] = 'true'

        # Basic heuristic: check if param name exists in URL -> path param
        path_params = {}
        query_params = {}
        json_body = {}
        
        formatted_url = url
        param_structure = param_structure or {}
        
        for key, value in params.items():
            location = param_structure.get(key)

            # If explicit location provided, use it
            if location == 'path':
                path_params[key] = value
                formatted_url = formatted_url.replace(f"{{{key}}}", str(value))
            elif location == 'query':
                query_params[key] = value
            elif location == 'body':
                if hasattr(value, "model_dump"):
                     json_body.update(value.model_dump())
                elif isinstance(value, dict):
                     json_body.update(value)
                else:
                     json_body[key] = value
            
            # Heuristic fallback
            elif f"{{{key}}}" in formatted_url:
                path_params[key] = value
                formatted_url = formatted_url.replace(f"{{{key}}}", str(value))
            elif method.upper() in ["GET", "DELETE"]:
                query_params[key] = value
            else:
                # For POST/PUT, if it's a model, use it as body. If it's a primitive, use it as json field.
                if hasattr(value, "model_dump"):
                     # Merge model fields into body
                     json_body.update(value.model_dump())
                elif isinstance(value, dict):
                     json_body.update(value)
                else:
                     json_body[key] = value
        
        async with httpx.AsyncClient() as client:
            req_kwargs = {}
            if query_params:
                req_kwargs["params"] = query_params
            if json_body:
                req_kwargs["json"] = json_body

            try:
                response = await client.request(
                    method=method,
                    url=formatted_url,
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
                logger.error(f"Error in MCP proxy call to {url}: {e}")
                raise e

    def build(self, router: APIRouter, transport: Literal['sse', 'streamable-http'] = 'streamable-http', mount_path: str = "/mcp"):
        """
        Build the MCP server by registering tools and mounting routes.
        """
        for item in self._registry:
            item_type = item.get("type", "local")
            func, mode, kwargs = item['func'], item['mode'], item['kwargs']
            
            if item_type == "proxy":
                url = item['url']
                method = item['method']
                param_structure = kwargs.get('param_structure', {})
                 
                if mode == MCPMode.TOOL:
                    tool_name = kwargs.get('name', func.__name__)
                    tool_desc = kwargs.get('description', func.__doc__ or "")
                    
                    # Use the function's signature directly
                    sig = inspect.signature(func)
                    
                    # Need to capture self, url, method in closure
                    async def proxy_wrapper(_url=url, _method=method, _structure=param_structure, **call_params):
                        return await self._external_proxy_call(_url, _method, call_params, _structure)
                    
                    proxy_wrapper.__doc__ = tool_desc
                    proxy_wrapper.__name__ = tool_name
                    proxy_wrapper.__signature__ = sig
                    proxy_wrapper.__annotations__ = func.__annotations__
                    
                    self.fastmcp.tool(name=tool_name, description=tool_desc)(proxy_wrapper)
                continue

            # Local FastAPI Route Logic
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