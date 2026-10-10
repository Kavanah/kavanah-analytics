"""
Seed a Chatwoot account with demo conversations, contacts, and messages.

Reads credentials from de_asset_chatwoot_accounts in Elasticsearch (same pattern
as get_account_reports.py) — no CLI credentials needed.

Usage:
  python3 /app/python/import.py chatwoot seed_demo_data
  python3 /app/python/import.py chatwoot seed_demo_data --conversations=30
  python3 /app/python/import.py chatwoot seed_demo_data --conversations=10 --inbox_name="Demo Inbox"
"""

import logging
import random
import time

import requests

import lib.logging
from lib.es_helpers import get_elasticsearch_client
from lib.encryption import decrypt

CHATWOOT_BASE_URL = "https://app.chatwoot.com"

FIRST_NAMES = [
    "Emma",
    "Liam",
    "Olivia",
    "Noah",
    "Ava",
    "Ethan",
    "Sophia",
    "Mason",
    "Isabella",
    "William",
    "Mia",
    "James",
    "Charlotte",
    "Oliver",
    "Amelia",
    "Benjamin",
    "Harper",
    "Elijah",
    "Evelyn",
    "Lucas",
    "Abigail",
    "Henry",
    "Emily",
    "Alexander",
    "Elizabeth",
    "Michael",
    "Sofia",
    "Daniel",
    "Avery",
    "Jackson",
    "Ella",
    "Sebastian",
    "Scarlett",
    "Aiden",
    "Grace",
    "Matthew",
]

LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Perez",
    "Thompson",
    "White",
    "Harris",
    "Sanchez",
    "Clark",
    "Ramirez",
    "Lewis",
    "Robinson",
    "Walker",
    "Young",
    "Allen",
    "King",
]

EMAIL_DOMAINS = [
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "protonmail.com",
    "live.com",
    "me.com",
    "mail.com",
]

INCOMING_MESSAGES = [
    "Hi, I need some help with my account.",
    "Hello! I have a question about your services.",
    "Can you help me understand how this works?",
    "I'm having trouble logging in.",
    "What are your business hours?",
    "I'd like to cancel my subscription.",
    "Can I get a refund for my last purchase?",
    "How do I update my billing information?",
    "I never received my order confirmation.",
    "The feature I need doesn't seem to be working.",
    "I accidentally sent a message to the wrong number.",
    "Can you explain the pricing plans?",
    "Is there a free trial available?",
    "I need to change the email on my account.",
    "When will my issue be resolved?",
    "I've been waiting for a response for days.",
    "This is urgent — please help.",
    "I can't find where to change my settings.",
    "My payment was declined but I was still charged.",
    "Can I speak to a manager?",
    "How do I export my data?",
    "I want to upgrade my plan.",
    "Do you support integration with other tools?",
    "I'm getting an error message every time I try to log in.",
    "Is there a way to bulk import contacts?",
]

FOLLOW_UP_MESSAGES = [
    "Actually, I have another question.",
    "That didn't work — can you try something else?",
    "OK I tried that and it still isn't working.",
    "Wait, I think I misunderstood. Let me explain again.",
    "Thanks for the info. One more thing though...",
    "Sorry for the confusion — here's more detail.",
    "I just tried that and got the same error.",
    "How long will this take?",
]

OUTGOING_MESSAGES = [
    "Hi! Thanks for reaching out. Happy to help — can you tell me more?",
    "Hello! Thanks for contacting us. Let me look into that for you.",
    "Of course! I'd be happy to assist. What specifically are you seeing?",
    "I understand. Let me check on that right away.",
    "Thanks for letting us know. I'll escalate this to our team.",
    "Great question! Here's how it works...",
    "I can definitely help with that. First, could you confirm your account email?",
    "I've checked your account and here's what I found.",
    "I've gone ahead and applied the fix on our end. Can you try again?",
    "Apologies for the inconvenience. This is a known issue we're working on.",
    "That should be resolved now. Please let me know if you see anything else.",
    "I've sent you a password reset email — please check your inbox.",
    "Your refund has been processed. It should appear in 3–5 business days.",
    "I've updated your billing details. Is there anything else I can help with?",
    "Thank you for your patience — we're looking into this.",
    "I've passed this on to our technical team for further investigation.",
    "You can find that setting under Account > Preferences > Notifications.",
    "Our business hours are Monday–Friday, 9am–6pm EST.",
    "We offer a 14-day free trial — no credit card required!",
    "I'll make sure this gets prioritized. We'll follow up within 24 hours.",
]

CLOSING_MESSAGES = [
    "Is there anything else I can help you with?",
    "Let me know if you have any other questions!",
    "Feel free to reach out anytime.",
    "Happy to help with anything else!",
    "Have a great day!",
]


def get_chatwoot_credentials(es):
    """Return (account_id, api_token) from the first Chatwoot account asset in ES."""
    try:
        result = es.search(
            index="de_asset_chatwoot_accounts",
            body={"query": {"match_all": {}}},
            size=1,
        )
        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            logging.error("No Chatwoot account assets found in de_asset_chatwoot_accounts.")
            return None, None
        account = hits[0]["_source"]
        account_id = account.get("chatwoot_account_id")
        encrypted_token = account.get("chatwoot_api_token")
        if not account_id or not encrypted_token:
            logging.error("Chatwoot account asset is missing account_id or api_token.")
            return None, None
        api_token = decrypt(encrypted_token)
        return account_id, api_token
    except Exception as e:
        logging.error(f"Error fetching Chatwoot credentials from ES: {e}")
        return None, None


def api_get(account_id, api_token, path, params=None):
    url = f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/{path}"
    headers = {"api_access_token": api_token}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_post(account_id, api_token, path, payload):
    url = f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/{path}"
    headers = {"api_access_token": api_token, "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_or_create_inbox(account_id, api_token, inbox_name):
    """Return inbox_id, creating the inbox if it doesn't exist."""
    try:
        data = api_get(account_id, api_token, "inboxes")
        for inbox in data.get("payload", []):
            if inbox.get("name") == inbox_name:
                logging.info(f"Using existing inbox '{inbox_name}' (id={inbox['id']})")
                return inbox["id"]
    except Exception as e:
        logging.warning(f"Could not list inboxes: {e}")

    logging.info(f"Creating inbox '{inbox_name}'...")
    try:
        result = api_post(
            account_id,
            api_token,
            "inboxes",
            {
                "name": inbox_name,
                "channel": {"type": "api", "webhook_url": ""},
            },
        )
        inbox_id = result.get("id")
        logging.info(f"Created inbox '{inbox_name}' (id={inbox_id})")
        return inbox_id
    except Exception as e:
        logging.error(f"Failed to create inbox: {e}")
        return None


def make_random_name():
    return random.choice(FIRST_NAMES), random.choice(LAST_NAMES)


def make_random_email(first, last):
    domain = random.choice(EMAIL_DOMAINS)
    suffix = random.randint(1, 999)
    return f"{first.lower()}.{last.lower()}{suffix}@{domain}"


def create_contact(account_id, api_token, first, last, email):
    try:
        result = api_post(
            account_id,
            api_token,
            "contacts",
            {
                "name": f"{first} {last}",
                "email": email,
            },
        )
        # Response is wrapped: {"payload": {"contact": {"id": ...}}}
        contact_id = result.get("payload", {}).get("contact", {}).get("id")
        return contact_id
    except Exception as e:
        logging.warning(f"Failed to create contact {first} {last}: {e}")
        return None


def create_conversation(account_id, api_token, contact_id, inbox_id):
    try:
        result = api_post(
            account_id,
            api_token,
            "conversations",
            {
                "inbox_id": inbox_id,
                "contact_id": contact_id,
            },
        )
        # Try top-level id first, then wrapped payload
        return result.get("id") or result.get("payload", {}).get("id")
    except Exception as e:
        logging.warning(f"Failed to create conversation for contact {contact_id}: {e}")
        return None


def send_message(account_id, api_token, conversation_id, content, message_type="incoming"):
    """message_type: 'incoming' (from contact) or 'outgoing' (from agent)."""
    try:
        api_post(
            account_id,
            api_token,
            f"conversations/{conversation_id}/messages",
            {
                "content": content,
                "message_type": message_type,
                "private": False,
            },
        )
    except Exception as e:
        logging.warning(f"Failed to send message to conversation {conversation_id}: {e}")


def resolve_conversation(account_id, api_token, conversation_id):
    try:
        url = f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/toggle_status"
        headers = {"api_access_token": api_token, "Content-Type": "application/json"}
        requests.post(url, headers=headers, json={"status": "resolved"}, timeout=30)
    except Exception as e:
        logging.warning(f"Failed to resolve conversation {conversation_id}: {e}")


def seed_conversation(account_id, api_token, inbox_id, resolve):
    first, last = make_random_name()
    email = make_random_email(first, last)

    contact_id = create_contact(account_id, api_token, first, last, email)
    if not contact_id:
        return False

    conversation_id = create_conversation(account_id, api_token, contact_id, inbox_id)
    if not conversation_id:
        return False

    # Opening incoming message
    send_message(
        account_id,
        api_token,
        conversation_id,
        random.choice(INCOMING_MESSAGES),
        "incoming",
    )
    time.sleep(0.1)

    # Agent reply
    send_message(
        account_id,
        api_token,
        conversation_id,
        random.choice(OUTGOING_MESSAGES),
        "outgoing",
    )
    time.sleep(0.1)

    # Optional follow-up exchange
    if random.random() < 0.5:
        send_message(
            account_id,
            api_token,
            conversation_id,
            random.choice(FOLLOW_UP_MESSAGES),
            "incoming",
        )
        time.sleep(0.1)
        send_message(
            account_id,
            api_token,
            conversation_id,
            random.choice(OUTGOING_MESSAGES),
            "outgoing",
        )
        time.sleep(0.1)

    # Closing message
    send_message(
        account_id,
        api_token,
        conversation_id,
        random.choice(CLOSING_MESSAGES),
        "outgoing",
    )
    time.sleep(0.1)

    if resolve:
        resolve_conversation(account_id, api_token, conversation_id)
        time.sleep(0.1)

    return True


def main(params):
    lib.logging.define("chatwoot-seed-demo-data")

    conversation_count = int(params.get("conversations", 30))
    inbox_name = params.get("inbox_name", "Demo Inbox")
    # ~70% of conversations get resolved
    resolve_rate = float(params.get("resolve_rate", 0.7))

    logging.info(f"Seeding {conversation_count} demo conversations into inbox '{inbox_name}'")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return

    account_id, api_token = get_chatwoot_credentials(es)
    if not account_id or not api_token:
        return

    logging.info(f"Using Chatwoot account ID: {account_id}")

    inbox_id = get_or_create_inbox(account_id, api_token, inbox_name)
    if not inbox_id:
        logging.error("Could not get or create inbox. Aborting.")
        return

    success = 0
    for i in range(conversation_count):
        resolve = random.random() < resolve_rate
        if seed_conversation(account_id, api_token, inbox_id, resolve):
            success += 1
            logging.info(f"  [{i+1}/{conversation_count}] Created conversation (resolved={resolve})")
        else:
            logging.warning(f"  [{i+1}/{conversation_count}] Failed to create conversation")
        # Brief pause to avoid rate limiting
        time.sleep(0.3)

    logging.info(f"Done. Created {success}/{conversation_count} conversations in inbox '{inbox_name}'.")
