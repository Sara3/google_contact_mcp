"""
MCP Google Contacts Server with per-user OAuth authentication.
Designed for hosting on Render.
"""
import os
import json
import secrets
from typing import Optional, Dict, Any

from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent

# OAuth configuration
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
SCOPES = [
    'https://www.googleapis.com/auth/contacts',
    'https://www.googleapis.com/auth/directory.readonly'
]

# In-memory token store
user_tokens: Dict[str, Dict[str, Any]] = {}
pending_states: Dict[str, str] = {}

# Initialize MCP server
mcp_server = Server("google-contacts")


def get_oauth_config():
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"{BASE_URL}/oauth/callback"]
        }
    }


def get_user_service(session_token: str):
    if session_token not in user_tokens:
        return None
    
    token_data = user_tokens[session_token]
    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES
    )
    return build('people', 'v1', credentials=creds)


# Register MCP tools
@mcp_server.list_tools()
async def list_tools():
    return [
        Tool(
            name="list_contacts",
            description="List all contacts or filter by name",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_token": {"type": "string", "description": "Your authentication session token"},
                    "name_filter": {"type": "string", "description": "Optional filter by name"},
                    "max_results": {"type": "integer", "description": "Max results", "default": 100}
                },
                "required": ["session_token"]
            }
        ),
        Tool(
            name="get_contact",
            description="Get a contact by resource name or email",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_token": {"type": "string", "description": "Your authentication session token"},
                    "identifier": {"type": "string", "description": "Resource name or email"}
                },
                "required": ["session_token", "identifier"]
            }
        ),
        Tool(
            name="create_contact",
            description="Create a new contact",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_token": {"type": "string", "description": "Your authentication session token"},
                    "given_name": {"type": "string", "description": "First name"},
                    "family_name": {"type": "string", "description": "Last name"},
                    "email": {"type": "string", "description": "Email address"},
                    "phone": {"type": "string", "description": "Phone number"}
                },
                "required": ["session_token", "given_name"]
            }
        ),
        Tool(
            name="delete_contact",
            description="Delete a contact",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_token": {"type": "string", "description": "Your authentication session token"},
                    "resource_name": {"type": "string", "description": "Contact resource name"}
                },
                "required": ["session_token", "resource_name"]
            }
        ),
        Tool(
            name="search_contacts",
            description="Search contacts by name, email, or phone",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_token": {"type": "string", "description": "Your authentication session token"},
                    "query": {"type": "string", "description": "Search term"},
                    "max_results": {"type": "integer", "description": "Max results", "default": 10}
                },
                "required": ["session_token", "query"]
            }
        )
    ]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    session_token = arguments.get("session_token", "")
    service = get_user_service(session_token)
    
    if not service:
        return [TextContent(type="text", text=f"Not authenticated. Visit {BASE_URL}/auth to login.")]
    
    try:
        if name == "list_contacts":
            results = service.people().connections().list(
                resourceName='people/me',
                pageSize=arguments.get("max_results", 100),
                personFields='names,emailAddresses,phoneNumbers',
                sortOrder='FIRST_NAME_ASCENDING'
            ).execute()
            
            contacts = results.get('connections', [])
            if not contacts:
                return [TextContent(type="text", text="No contacts found.")]
            
            name_filter = arguments.get("name_filter", "").lower() if arguments.get("name_filter") else ""
            output = []
            for person in contacts:
                names = person.get('names', [])
                if not names:
                    continue
                display_name = names[0].get('displayName', 'Unknown')
                if name_filter and name_filter not in display_name.lower():
                    continue
                emails = person.get('emailAddresses', [])
                email = emails[0].get('value', '') if emails else ''
                phones = person.get('phoneNumbers', [])
                phone = phones[0].get('value', '') if phones else ''
                output.append(f"- {display_name} | {email} | {phone}")
            
            return [TextContent(type="text", text=f"Found {len(output)} contacts:\n" + "\n".join(output))]
        
        elif name == "get_contact":
            identifier = arguments.get("identifier", "")
            if identifier.startswith('people/'):
                person = service.people().get(
                    resourceName=identifier,
                    personFields='names,emailAddresses,phoneNumbers'
                ).execute()
            else:
                results = service.people().connections().list(
                    resourceName='people/me',
                    pageSize=100,
                    personFields='names,emailAddresses,phoneNumbers'
                ).execute()
                person = None
                for p in results.get('connections', []):
                    emails = p.get('emailAddresses', [])
                    if emails and emails[0].get('value') == identifier:
                        person = p
                        break
                if not person:
                    return [TextContent(type="text", text=f"Contact not found: {identifier}")]
            
            names = person.get('names', [{}])[0]
            emails = person.get('emailAddresses', [])
            phones = person.get('phoneNumbers', [])
            text = f"Name: {names.get('displayName', 'Unknown')}\nEmail: {emails[0].get('value') if emails else 'N/A'}\nPhone: {phones[0].get('value') if phones else 'N/A'}"
            return [TextContent(type="text", text=text)]
        
        elif name == "create_contact":
            body = {'names': [{'givenName': arguments.get('given_name'), 'familyName': arguments.get('family_name', '')}]}
            if arguments.get('email'):
                body['emailAddresses'] = [{'value': arguments['email']}]
            if arguments.get('phone'):
                body['phoneNumbers'] = [{'value': arguments['phone']}]
            person = service.people().createContact(body=body).execute()
            return [TextContent(type="text", text=f"Contact created: {person.get('resourceName')}")]
        
        elif name == "delete_contact":
            service.people().deleteContact(resourceName=arguments.get('resource_name')).execute()
            return [TextContent(type="text", text=f"Contact deleted.")]
        
        elif name == "search_contacts":
            results = service.people().connections().list(
                resourceName='people/me',
                pageSize=100,
                personFields='names,emailAddresses,phoneNumbers'
            ).execute()
            
            query = arguments.get("query", "").lower()
            max_results = arguments.get("max_results", 10)
            matches = []
            
            for person in results.get('connections', []):
                names = person.get('names', [])
                name = names[0].get('displayName', '') if names else ''
                emails = person.get('emailAddresses', [])
                email = emails[0].get('value', '') if emails else ''
                phones = person.get('phoneNumbers', [])
                phone = phones[0].get('value', '') if phones else ''
                
                if query in name.lower() or query in email.lower() or query in phone:
                    matches.append(f"- {name} | {email} | {phone}")
                    if len(matches) >= max_results:
                        break
            
            if not matches:
                return [TextContent(type="text", text=f"No contacts matching '{query}'.")]
            return [TextContent(type="text", text=f"Found {len(matches)} matches:\n" + "\n".join(matches))]
        
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


# HTTP endpoints
async def homepage(request: Request):
    return JSONResponse({
        "service": "Google Contacts MCP Server",
        "status": "running",
        "auth_url": f"{BASE_URL}/auth",
        "mcp_sse_url": f"{BASE_URL}/sse"
    })


async def start_auth(request: Request):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return JSONResponse({"error": "OAuth not configured"}, status_code=500)
    
    redirect_uri = request.query_params.get("redirect_uri", BASE_URL)
    
    flow = Flow.from_client_config(get_oauth_config(), scopes=SCOPES)
    flow.redirect_uri = f"{BASE_URL}/oauth/callback"
    
    state = secrets.token_urlsafe(32)
    pending_states[state] = redirect_uri
    
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
        state=state
    )
    return RedirectResponse(auth_url)


async def oauth_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    
    if not code or state not in pending_states:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    
    pending_states.pop(state)
    
    flow = Flow.from_client_config(get_oauth_config(), scopes=SCOPES)
    flow.redirect_uri = f"{BASE_URL}/oauth/callback"
    flow.fetch_token(code=code)
    creds = flow.credentials
    
    service = build('people', 'v1', credentials=creds)
    profile = service.people().get(resourceName='people/me', personFields='emailAddresses').execute()
    
    emails = profile.get('emailAddresses', [])
    user_email = emails[0]['value'] if emails else "unknown"
    
    session_token = secrets.token_urlsafe(32)
    user_tokens[session_token] = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "email": user_email
    }
    
    return JSONResponse({
        "status": "authenticated",
        "email": user_email,
        "session_token": session_token,
        "mcp_sse_url": f"{BASE_URL}/sse",
        "message": "Use session_token when calling MCP tools"
    })


async def check_status(request: Request):
    session_token = request.path_params.get("session_token")
    if session_token in user_tokens:
        return JSONResponse({"authenticated": True, "email": user_tokens[session_token].get("email")})
    return JSONResponse({"authenticated": False})


# SSE endpoint for MCP
sse_transport = SseServerTransport("/messages/")


async def handle_sse(request: Request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options()
        )


async def handle_messages(request: Request):
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)


# Build app
app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/auth", start_auth),
        Route("/oauth/callback", oauth_callback),
        Route("/status/{session_token}", check_status),
        Route("/sse", handle_sse),
        Route("/messages/", handle_messages, methods=["POST"]),
    ]
)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
