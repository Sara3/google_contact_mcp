"""
MCP Google Contacts Server with per-user OAuth authentication.
Designed for hosting on Render.
"""
import os
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

# Initialize MCP server and SSE transport
mcp_server = Server("google-contacts")
sse = SseServerTransport("/messages/")

# All available person fields for complete contact data
PERSON_FIELDS = ','.join([
    'names',
    'emailAddresses', 
    'phoneNumbers',
    'addresses',
    'birthdays',
    'organizations',
    'photos',
    'biographies',
    'urls',
    'relations',
    'events',
    'memberships'
])


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
            description="List all contacts or filter by name. First authenticate at " + BASE_URL + "/auth",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_token": {"type": "string", "description": "Your authentication session token from /auth"},
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


def format_contact(person: dict) -> str:
    """Format a contact with all available fields."""
    lines = []
    
    # Name
    names = person.get('names', [])
    if names:
        lines.append(f"Name: {names[0].get('displayName', 'Unknown')}")
    
    # Resource name (ID)
    lines.append(f"ID: {person.get('resourceName', 'N/A')}")
    
    # Emails
    emails = person.get('emailAddresses', [])
    if emails:
        email_list = [e.get('value', '') for e in emails]
        lines.append(f"Email: {', '.join(email_list)}")
    
    # Phones
    phones = person.get('phoneNumbers', [])
    if phones:
        phone_list = [p.get('value', '') for p in phones]
        lines.append(f"Phone: {', '.join(phone_list)}")
    
    # Addresses
    addresses = person.get('addresses', [])
    if addresses:
        for addr in addresses:
            formatted = addr.get('formattedValue', '')
            addr_type = addr.get('type', '')
            if formatted:
                lines.append(f"Address ({addr_type}): {formatted}")
    
    # Birthday
    birthdays = person.get('birthdays', [])
    if birthdays:
        bday = birthdays[0].get('date', {})
        if bday:
            month = bday.get('month', '')
            day = bday.get('day', '')
            year = bday.get('year', '')
            bday_str = f"{month}/{day}" + (f"/{year}" if year else "")
            lines.append(f"Birthday: {bday_str}")
    
    # Organizations
    orgs = person.get('organizations', [])
    if orgs:
        for org in orgs:
            org_name = org.get('name', '')
            title = org.get('title', '')
            if org_name or title:
                lines.append(f"Organization: {org_name}" + (f" ({title})" if title else ""))
    
    return '\n'.join(lines)


def get_all_contacts(service, person_fields: str = PERSON_FIELDS):
    """Fetch ALL contacts with pagination."""
    all_contacts = []
    page_token = None
    
    while True:
        results = service.people().connections().list(
            resourceName='people/me',
            pageSize=1000,  # Max allowed
            personFields=person_fields,
            pageToken=page_token,
            sortOrder='FIRST_NAME_ASCENDING'
        ).execute()
        
        contacts = results.get('connections', [])
        all_contacts.extend(contacts)
        
        page_token = results.get('nextPageToken')
        if not page_token:
            break
    
    return all_contacts


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    session_token = arguments.get("session_token", "")
    service = get_user_service(session_token)
    
    if not service:
        return [TextContent(type="text", text=f"Not authenticated. Visit {BASE_URL}/auth to login with your Google account first.")]
    
    try:
        if name == "list_contacts":
            # Get ALL contacts with pagination
            all_contacts = get_all_contacts(service)
            
            if not all_contacts:
                return [TextContent(type="text", text="No contacts found.")]
            
            name_filter = (arguments.get("name_filter") or "").lower()
            max_results = arguments.get("max_results", 100)
            output = []
            
            for person in all_contacts:
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
                
                # Include birthday if available
                birthdays = person.get('birthdays', [])
                bday_str = ""
                if birthdays:
                    bday = birthdays[0].get('date', {})
                    if bday:
                        bday_str = f" | Bday: {bday.get('month', '')}/{bday.get('day', '')}"
                
                output.append(f"- {display_name} | {email} | {phone}{bday_str}")
                
                if len(output) >= max_results:
                    break
            
            total = len(all_contacts)
            shown = len(output)
            return [TextContent(type="text", text=f"Found {total} total contacts (showing {shown}):\n" + "\n".join(output))]
        
        elif name == "get_contact":
            identifier = arguments.get("identifier", "")
            person = None
            
            if identifier.startswith('people/'):
                person = service.people().get(
                    resourceName=identifier,
                    personFields=PERSON_FIELDS
                ).execute()
            else:
                # Search by email or name
                all_contacts = get_all_contacts(service)
                identifier_lower = identifier.lower()
                
                for p in all_contacts:
                    # Check emails
                    emails = p.get('emailAddresses', [])
                    for email in emails:
                        if email.get('value', '').lower() == identifier_lower:
                            person = p
                            break
                    if person:
                        break
                    
                    # Check name
                    names = p.get('names', [])
                    if names and identifier_lower in names[0].get('displayName', '').lower():
                        person = p
                        break
                
                if not person:
                    return [TextContent(type="text", text=f"Contact not found: {identifier}")]
            
            return [TextContent(type="text", text=format_contact(person))]
        
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
            return [TextContent(type="text", text="Contact deleted.")]
        
        elif name == "search_contacts":
            all_contacts = get_all_contacts(service)
            
            query = arguments.get("query", "").lower()
            max_results = arguments.get("max_results", 10)
            matches = []
            
            for person in all_contacts:
                names = person.get('names', [])
                name = names[0].get('displayName', '') if names else ''
                
                emails = person.get('emailAddresses', [])
                email_values = [e.get('value', '').lower() for e in emails]
                
                phones = person.get('phoneNumbers', [])
                phone_values = [p.get('value', '') for p in phones]
                
                # Search in all fields
                match = False
                if query in name.lower():
                    match = True
                elif any(query in e for e in email_values):
                    match = True
                elif any(query in p for p in phone_values):
                    match = True
                
                if match:
                    matches.append(format_contact(person))
                    if len(matches) >= max_results:
                        break
            
            if not matches:
                return [TextContent(type="text", text=f"No contacts matching '{query}'.")]
            return [TextContent(type="text", text=f"Found {len(matches)} matches:\n\n" + "\n\n---\n\n".join(matches))]
        
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


# HTTP route handlers
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


# SSE endpoint - raw ASGI handler
async def handle_sse(scope, receive, send):
    """Handle SSE connections for MCP."""
    async with sse.connect_sse(scope, receive, send) as streams:
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options()
        )


# Messages endpoint - raw ASGI handler  
async def handle_messages(scope, receive, send):
    """Handle POST messages for MCP."""
    await sse.handle_post_message(scope, receive, send)


# Build the Starlette app with routes
app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/auth", start_auth),
        Route("/oauth/callback", oauth_callback),
        Route("/status/{session_token}", check_status),
    ]
)


# Create a combined ASGI app that handles both Starlette routes and raw SSE endpoints
async def combined_app(scope, receive, send):
    path = scope.get("path", "")
    
    if path == "/sse":
        await handle_sse(scope, receive, send)
    elif path.startswith("/messages"):
        await handle_messages(scope, receive, send)
    else:
        await app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(combined_app, host="0.0.0.0", port=port)
