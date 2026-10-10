"""
Elasticsearch Authentication Helper
Provides authentication and authorization against Elasticsearch Security API.
"""

import os
import ssl
import logging
from elasticsearch import Elasticsearch
import lib.logging


def authenticate_user(username: str, password: str) -> dict:
    """
    Authenticate user against Elasticsearch Security API.

    Args:
        username: Elasticsearch username
        password: Elasticsearch password

    Returns:
        dict: {
            'success': bool,
            'username': str,
            'roles': list,
            'full_name': str,
            'email': str,
            'error': str  # if success=False
        }
    """
    lib.logging.define("es-auth")

    try:
        # Create ES client with user's credentials
        es_client = get_elasticsearch_client_for_auth(username, password)

        if es_client is None:
            logging.error(f"Failed to create ES client for user: {username}")
            return {"success": False, "error": "Unable to connect to Elasticsearch"}

        # Authenticate user using ES Security API
        try:
            auth_response = es_client.security.authenticate()
        except Exception as auth_error:
            logging.warning(f"Authentication failed for user '{username}': {auth_error}")
            return {"success": False, "error": "Invalid credentials"}

        # Extract user information
        username_from_es = auth_response.get("username", username)
        roles = auth_response.get("roles", [])
        full_name = auth_response.get("full_name", "")
        email = auth_response.get("email", "")

        logging.info(f"AUDIT: user={username} action=authenticate success=True roles={roles}")

        return {
            "success": True,
            "username": username_from_es,
            "roles": roles,
            "full_name": full_name,
            "email": email,
        }

    except Exception as e:
        logging.error(f"AUDIT: user={username} action=authenticate success=False error={str(e)}")
        return {"success": False, "error": str(e)}


def has_role(username: str, password: str, required_role: str) -> bool:
    """
    Check if user has a specific role.

    Args:
        username: Elasticsearch username
        password: Elasticsearch password
        required_role: Role name to check for

    Returns:
        bool: True if user has the role, False otherwise
    """
    lib.logging.define("es-auth")

    auth_result = authenticate_user(username, password)

    if not auth_result["success"]:
        return False

    has_required_role = required_role in auth_result["roles"]

    if not has_required_role:
        logging.warning(f"AUDIT: user={username} action=role_check required_role={required_role} success=False")
    else:
        logging.info(f"AUDIT: user={username} action=role_check required_role={required_role} success=True")

    return has_required_role


def get_elasticsearch_client_for_auth(username: str, password: str):
    """
    Get Elasticsearch client authenticated as specific user.
    Only for authentication checks, not data operations.

    Args:
        username: Elasticsearch username
        password: Elasticsearch password

    Returns:
        Elasticsearch client or None if connection fails
    """
    lib.logging.define("es-auth")

    es_ip = os.environ.get("ES01_INTERNAL_IP")
    use_ssl = os.environ.get("ES_USE_SSL", "true").lower() != "false"

    if not use_ssl:
        try:
            es = Elasticsearch([f"http://{es_ip}:9200"], basic_auth=(username, password))
            return es
        except Exception as e:
            logging.error(f"Error connecting to Elasticsearch with user credentials: {e}")
            return None

    # Create SSL context with relaxed X509 strict verification
    # (same as es_helpers.py)
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT

    # Elasticsearch connection configuration
    es_url = f"https://{es_ip}:9200"
    ca_cert_path = "/app/data/certs/es01_http_ca.crt"

    # Check if CA certificate exists
    if not os.path.exists(ca_cert_path):
        logging.error(f"CA certificate not found at: {ca_cert_path}")
        return None

    # Load the CA certificate into the SSL context
    try:
        ctx.load_verify_locations(cafile=ca_cert_path)
    except Exception as e:
        logging.error(f"Error loading CA certificate: {e}")
        return None

    # Connect to Elasticsearch with provided credentials
    try:
        es = Elasticsearch(
            [es_url],
            basic_auth=(username, password),
            ssl_show_warn=False,
            verify_certs=True,
            ssl_context=ctx,
        )

        return es

    except Exception as e:
        logging.error(f"Error connecting to Elasticsearch with user credentials: {e}")
        return None
