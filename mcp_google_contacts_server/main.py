"""
MCP Google Contacts Server with per-user OAuth authentication.
Designed for hosting on Render.
"""
import os
import json
import secrets
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

# OAuth configuration
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
SCOPES = [
    'https://www.googleapis.com/auth/contacts',
    'https://www.googleapis.com/auth/directory.readonly'
]

# In-memory token store (use Redis in production for persistence)
user_tokens: Dict[str, Dict[str, Any]] = {}
pending_states: Dict[str, str] = {}  # state -> redirect_uri

# Initialize FastAPI app
app = FastAPI(title="Google Contacts MCP Server")

# Initialize MCP server
mcp = FastMCP("google-contacts")


def get_oauth_config():
    """Get OAuth client configuration."""
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"{BASE_URL}/oauth/callback"]
        }
    }


def get_user_service(user_id: str):
    """Get Google Contacts service for a specific user."""
    if user_id not in user_tokens:
        return None
    
    token_data = user_tokens[user_id]
    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES
    )
    
    return build('people', 'v1', credentials=creds)


@app.get("/")
async def root():
    """Health check and info endpoint."""
    return {
        "service": "Google Contacts MCP Server",
        "status": "running",
        "auth_url": f"{BASE_URL}/auth",
        "mcp_url": f"{BASE_URL}/mcp"
    }


@app.get("/auth")
async def start_auth(redirect_uri: Optional[str] = None):
    """Start OAuth flow - redirects user to Google login."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(500, "OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
    
    flow = Flow.from_client_config(get_oauth_config(), scopes=SCOPES)
    flow.redirect_uri = f"{BASE_URL}/oauth/callback"
    
    state = secrets.token_urlsafe(32)
    pending_states[state] = redirect_uri or BASE_URL
    
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
        state=state
    )
    
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str):
    """Handle OAuth callback from Google."""
    if state not in pending_states:
        raise HTTPException(400, "Invalid state parameter")
    
    redirect_uri = pending_states.pop(state)
    
    flow = Flow.from_client_config(get_oauth_config(), scopes=SCOPES)
    flow.redirect_uri = f"{BASE_URL}/oauth/callback"
    
    flow.fetch_token(code=code)
    creds = flow.credentials
    
    # Get user info to use as user_id
    service = build('people', 'v1', credentials=creds)
    profile = service.people().get(resourceName='people/me', personFields='emailAddresses').execute()
    
    emails = profile.get('emailAddresses', [])
    user_email = emails[0]['value'] if emails else profile.get('resourceName')
    
    # Store tokens
    user_tokens[user_email] = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "email": user_email
    }
    
    # Generate a simple session token
    session_token = secrets.token_urlsafe(32)
    user_tokens[session_token] = user_tokens[user_email]
    
    return JSONResponse({
        "status": "authenticated",
        "email": user_email,
        "session_token": session_token,
        "mcp_url": f"{BASE_URL}/mcp",
        "message": "Use the session_token in your MCP client configuration"
    })


@app.get("/status/{session_token}")
async def check_status(session_token: str):
    """Check if a session is authenticated."""
    if session_token in user_tokens:
        return {"authenticated": True, "email": user_tokens[session_token].get("email")}
    return {"authenticated": False}


# MCP Tools - these require a session_token parameter

@mcp.tool()
async def list_contacts(session_token: str, name_filter: Optional[str] = None, max_results: int = 100) -> str:
    """List all contacts or filter by name.
    
    Args:
        session_token: Your authentication session token
        name_filter: Optional filter to find contacts by name
        max_results: Maximum number of results to return
    """
    service = get_user_service(session_token)
    if not service:
        return f"Not authenticated. Visit {BASE_URL}/auth to login with your Google account."
    
    try:
        results = service.people().connections().list(
            resourceName='people/me',
            pageSize=max_results,
            personFields='names,emailAddresses,phoneNumbers',
            sortOrder='FIRST_NAME_ASCENDING'
        ).execute()
        
        contacts = results.get('connections', [])
        if not contacts:
            return "No contacts found."
        
        output = []
        for person in contacts:
            names = person.get('names', [])
            if not names:
                continue
            name = names[0].get('displayName', 'Unknown')
            
            if name_filter and name_filter.lower() not in name.lower():
                continue
            
            emails = person.get('emailAddresses', [])
            email = emails[0].get('value', '') if emails else ''
            
            phones = person.get('phoneNumbers', [])
            phone = phones[0].get('value', '') if phones else ''
            
            output.append(f"- {name} | {email} | {phone}")
        
        return f"Found {len(output)} contacts:\n" + "\n".join(output)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def get_contact(session_token: str, identifier: str) -> str:
    """Get a contact by resource name or email.
    
    Args:
        session_token: Your authentication session token
        identifier: Resource name (people/*) or email address
    """
    service = get_user_service(session_token)
    if not service:
        return f"Not authenticated. Visit {BASE_URL}/auth to login."
    
    try:
        if identifier.startswith('people/'):
            person = service.people().get(
                resourceName=identifier,
                personFields='names,emailAddresses,phoneNumbers'
            ).execute()
        else:
            # Search by email
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
                return f"Contact with email {identifier} not found."
        
        names = person.get('names', [{}])[0]
        emails = person.get('emailAddresses', [])
        phones = person.get('phoneNumbers', [])
        
        return f"""Contact Details:
Name: {names.get('displayName', 'Unknown')}
Email: {emails[0].get('value') if emails else 'N/A'}
Phone: {phones[0].get('value') if phones else 'N/A'}
Resource: {person.get('resourceName')}"""
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def create_contact(session_token: str, given_name: str, family_name: Optional[str] = None,
                        email: Optional[str] = None, phone: Optional[str] = None) -> str:
    """Create a new contact.
    
    Args:
        session_token: Your authentication session token
        given_name: First name
        family_name: Last name
        email: Email address
        phone: Phone number
    """
    service = get_user_service(session_token)
    if not service:
        return f"Not authenticated. Visit {BASE_URL}/auth to login."
    
    try:
        body = {
            'names': [{'givenName': given_name, 'familyName': family_name or ''}]
        }
        if email:
            body['emailAddresses'] = [{'value': email}]
        if phone:
            body['phoneNumbers'] = [{'value': phone}]
        
        person = service.people().createContact(body=body).execute()
        return f"Contact created: {person.get('resourceName')}"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def delete_contact(session_token: str, resource_name: str) -> str:
    """Delete a contact.
    
    Args:
        session_token: Your authentication session token
        resource_name: Contact resource name (people/*)
    """
    service = get_user_service(session_token)
    if not service:
        return f"Not authenticated. Visit {BASE_URL}/auth to login."
    
    try:
        service.people().deleteContact(resourceName=resource_name).execute()
        return f"Contact {resource_name} deleted."
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def search_contacts(session_token: str, query: str, max_results: int = 10) -> str:
    """Search contacts by name, email, or phone.
    
    Args:
        session_token: Your authentication session token
        query: Search term
        max_results: Maximum results to return
    """
    service = get_user_service(session_token)
    if not service:
        return f"Not authenticated. Visit {BASE_URL}/auth to login."
    
    try:
        results = service.people().connections().list(
            resourceName='people/me',
            pageSize=100,
            personFields='names,emailAddresses,phoneNumbers'
        ).execute()
        
        query_lower = query.lower()
        matches = []
        
        for person in results.get('connections', []):
            names = person.get('names', [])
            name = names[0].get('displayName', '') if names else ''
            emails = person.get('emailAddresses', [])
            email = emails[0].get('value', '') if emails else ''
            phones = person.get('phoneNumbers', [])
            phone = phones[0].get('value', '') if phones else ''
            
            if (query_lower in name.lower() or 
                query_lower in email.lower() or 
                query_lower in phone):
                matches.append(f"- {name} | {email} | {phone}")
                if len(matches) >= max_results:
                    break
        
        if not matches:
            return f"No contacts matching '{query}'."
        
        return f"Found {len(matches)} matches:\n" + "\n".join(matches)
    except Exception as e:
        return f"Error: {str(e)}"


# Mount MCP server
app.mount("/mcp", mcp.http_app())


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
