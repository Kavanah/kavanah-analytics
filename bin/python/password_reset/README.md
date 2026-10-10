# Kibana/Elasticsearch Password Reset App

A self-hosted password reset solution for Kibana/Elasticsearch users. Allows users to reset their passwords via email link without requiring admin intervention.

## Features

- **User-facing password reset flow** - Users enter username, receive email with reset link
- **Admin configuration panel** - Configure SMTP settings via web UI (no hardcoded credentials)
- **Secure token generation** - Cryptographically secure, single-use, time-limited tokens
- **Email masking** - User emails are masked in the UI for privacy
- **Graceful fallback** - Users without email on file are directed to contact admin
- **Docker-ready** - Designed to run in containerized environments

## Architecture

```
User clicks "Forgot Password"
    → Enters username
    → App checks Elasticsearch for user's email
    → Generates secure token (stored in config/tokens.json)
    → Sends reset email via SMTP (e.g., Amazon SES)
    → User clicks link
    → App validates token
    → User enters new password
    → App calls ES API to change password
```

## Installation

### Prerequisites

- Python 3.9+
- Access to Elasticsearch with admin credentials
- SMTP service (e.g., Amazon SES, SendGrid, Mailgun)

### Option 1: Run Standalone

```bash
# Clone or copy the password_reset directory
cd password_reset

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export ES_HOST="https://localhost:9200"
export ES_USER="elastic"
export ES_PASS="your_elastic_password"
export APP_SECRET_KEY="generate_a_random_32_char_string"
export ADMIN_PASSWORD="initial_admin_password"

# Run the app
python app.py
```

### Option 2: Run with Gunicorn (Production)

```bash
gunicorn -w 2 -b 0.0.0.0:8085 app:app
```

### Option 3: Docker Container

Add to your `docker-compose.yml`:

```yaml
password_reset:
  image: python:3.13.0
  container_name: password_reset
  volumes:
    - "./bin/python/password_reset:/app"
  working_dir: /app
  environment:
    ES_HOST: "https://172.18.0.2:9200"
    ES_USER: "elastic"
    ES_PASS: "your_elastic_password"
    APP_SECRET_KEY: "generate_a_random_32_char_string"
    ADMIN_PASSWORD: "initial_admin_password"
  command: >
    bash -c "pip install -r requirements.txt --break-system-packages && 
             gunicorn -w 2 -b 0.0.0.0:8085 app:app"
  ports:
    - "8085:8085"
  networks:
    elastic-network:
      ipv4_address: "172.18.0.6"
  restart: always
```

### Option 4: Add to Existing Container

If you have an existing Python container (like `imports_container`), add to your startup script:

```bash
# In start_apps.sh or similar
cd /app/password_reset
gunicorn -w 2 -b 0.0.0.0:8085 app:app &
```

## Nginx Configuration

Add a location block to proxy requests to the app:

```nginx
location /reset-password/ {
    proxy_pass http://172.18.0.5:8085/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ES_HOST` | Yes | Elasticsearch URL (e.g., `https://172.18.0.2:9200`) |
| `ES_USER` | Yes | Elasticsearch admin username |
| `ES_PASS` | Yes | Elasticsearch admin password |
| `APP_SECRET_KEY` | No | Flask session secret (auto-generated if not set) |
| `ADMIN_PASSWORD` | Yes | Initial admin password for first login |

### Admin Configuration (via Web UI)

Access the admin panel at `/admin/login` and configure:

- **SMTP Host** - Your SMTP server (e.g., `email-smtp.us-east-1.amazonaws.com`)
- **SMTP Port** - Usually 587 (TLS) or 465 (SSL)
- **SMTP Username** - Your SMTP username or API key
- **SMTP Password** - Your SMTP password
- **Use TLS** - Enable for port 587
- **From Email** - Sender email address (must be verified in your SMTP service)
- **App URL** - Full URL where this app is accessible
- **Admin Contact** - Email shown to users without email on file
- **Token Expiry** - How long reset links are valid (minutes)

### Amazon SES Example Settings

```
SMTP Host: email-smtp.us-east-1.amazonaws.com
SMTP Port: 587
SMTP Username: (Your SES SMTP username from AWS Console)
SMTP Password: (Your SES SMTP password from AWS Console)
Use TLS: Yes
From Email: noreply@yourdomain.com (must be verified in SES)
```

## Adding Kibana Login Link

Add a "Forgot Password" link to the Kibana login page:

In `kibana.yml`:
```yaml
xpack.security.loginAssistanceMessage: "Forgot your password? <a href='https://your-domain.com/reset-password/'>Click here to reset</a>"
```

Then restart Kibana.

## Security Considerations

- **Tokens are single-use** - Once used, tokens are invalidated
- **Tokens expire** - Default 60 minutes, configurable
- **Rate limiting** - Consider adding nginx rate limiting for production
- **HTTPS required** - Always use HTTPS in production
- **Credentials storage** - SMTP credentials stored in `config/settings.json` (ensure proper file permissions)

## File Structure

```
password_reset/
├── app.py                 # Main Flask application
├── requirements.txt       # Python dependencies
├── README.md              # This file
├── config/
│   ├── settings.json      # SMTP and app settings (created by admin UI)
│   └── tokens.json        # Active reset tokens (auto-managed)
└── templates/
    ├── base.html          # Base template with styling
    ├── request_reset.html # Username entry form
    ├── reset_password.html# New password form
    ├── success.html       # Success message
    └── admin/
        ├── login.html     # Admin login
        └── settings.html  # Settings configuration
```

## Elasticsearch User Requirements

For password reset to work, users must have an email address set in Elasticsearch:

```bash
# Creating a user with email
curl -k -u elastic:PASSWORD -X POST "https://localhost:9200/_security/user/username" \
  -H "Content-Type: application/json" -d '{
  "password": "initial_password",
  "roles": ["asset_admin"],
  "full_name": "User Name",
  "email": "user@example.com"
}'

# Adding email to existing user
curl -k -u elastic:PASSWORD -X PUT "https://localhost:9200/_security/user/username" \
  -H "Content-Type: application/json" -d '{
  "email": "user@example.com"
}'
```

## Troubleshooting

### "ADMIN_PASSWORD environment variable not set"
Set the `ADMIN_PASSWORD` environment variable before starting the app.

### "No email address on file"
The user doesn't have an email in Elasticsearch. Add one using the ES API.

### "Error sending email"
Check SMTP settings in admin panel. Use the "Test Email" feature to debug.

### Token expired
Tokens expire after the configured time (default 60 minutes). Request a new reset link.

## License

MIT License - Feel free to use and modify for your organization.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Support

For issues specific to this app, contact your system administrator.
