from enum import Enum

class MCPMode(str, Enum):
    TOOL = 'tool'
    RESOURCE = 'resource'
    PROMPT = 'prompt'

