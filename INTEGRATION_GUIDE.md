# MSD Congress Activity Dashboard — Event Cadence Integration Guide

## Overview

The dashboard is embedded in Event Cadence as an `<iframe>`. Access is controlled by a
short-lived JWT that Event Cadence generates server-side and passes to the iframe. The
dashboard validates the token on every API request. No data is served without a valid token.

---

## 1. Shared Secret Setup

Both Event Cadence and the dashboard server need the **same secret**:

```
JWT_SECRET=your-strong-random-secret-here
```

- **Event Cadence**: set as an environment variable / app setting
- **Dashboard server**: set as `JWT_SECRET` environment variable (or Azure Key Vault reference)

Generate a strong secret:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

> **Never hardcode the secret in source code.** The default `dev-secret-change-in-production`
> in server.py is for local development only.

---

## 2. Production Server Settings

Set these environment variables on the dashboard server:

| Variable | Value | Notes |
|---|---|---|
| `JWT_SECRET` | your shared secret | Must match Event Cadence |
| `REQUIRE_AUTH` | `true` | Always true in production |
| `ALLOW_TOKEN_GENERATION` | `false` | Disable the dev token endpoint |

---

## 3. Generating a Token (Event Cadence side)

When a user navigates to the page containing the dashboard iframe, your server generates
a short-lived JWT and injects it into the iframe `src`.

### C# / .NET (SQL Server / Event Cadence stack)

```csharp
using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using Microsoft.IdentityModel.Tokens;

public string GenerateDashboardToken(string userEmail, string role = "viewer")
{
    var secret  = Environment.GetEnvironmentVariable("JWT_SECRET");
    var key     = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(secret));
    var creds   = new SigningCredentials(key, SecurityAlgorithms.HmacSha256);

    var claims = new[]
    {
        new Claim(JwtRegisteredClaimNames.Sub, userEmail),
        new Claim("role", role),
    };

    var token = new JwtSecurityToken(
        claims:  claims,
        expires: DateTime.UtcNow.AddMinutes(60),   // 60-minute window
        signingCredentials: creds
    );

    return new JwtSecurityTokenHandler().WriteToken(token);
}
```

### Node.js (if applicable)

```javascript
const jwt = require('jsonwebtoken');

function generateDashboardToken(userEmail, role = 'viewer') {
  return jwt.sign(
    { sub: userEmail, role },
    process.env.JWT_SECRET,
    { expiresIn: '60m' }
  );
}
```

### Python (if applicable)

```python
from jose import jwt
from datetime import datetime, timedelta
import os

def generate_dashboard_token(user_email: str, role: str = "viewer") -> str:
    payload = {
        "sub":  user_email,
        "role": role,
        "exp":  datetime.utcnow() + timedelta(minutes=60),
    }
    return jwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")
```

---

## 4. Embedding the Dashboard

### Option A — Token in URL (simplest)

```html
<!-- In your Event Cadence page/view -->
<iframe
  src="https://dashboard.yourdomain.com/?token={{ dashboardToken }}"
  width="100%"
  height="100%"
  frameborder="0"
  allow="fullscreen"
></iframe>
```

The dashboard strips the token from the URL bar immediately after reading it, so it
never appears in browser history or server logs.

### Option B — postMessage (more secure, no token in URL)

```html
<iframe id="dashboard-frame"
  src="https://dashboard.yourdomain.com/"
  width="100%"
  height="100%"
  frameborder="0"
></iframe>

<script>
  const frame = document.getElementById('dashboard-frame');
  frame.addEventListener('load', () => {
    frame.contentWindow.postMessage(
      { type: 'MSD_AUTH_TOKEN', token: '{{ dashboardToken }}' },
      'https://dashboard.yourdomain.com'
    );
  });
</script>
```

The dashboard listens for `MSD_AUTH_TOKEN` messages and stores the token in memory only.

---

## 5. Token Payload

| Field | Type | Description |
|---|---|---|
| `sub` | string | User email address |
| `role` | string | `admin` or `viewer` (viewer is default) |
| `exp` | unix timestamp | Expiry — recommend 60 minutes |

---

## 6. Token Expiry & Refresh

The dashboard shows a friendly "Session Expired" screen if the token is rejected.
The user is prompted to re-open the dashboard from Event Cadence, which generates a
fresh token automatically.

For a seamless experience, you can refresh the token before expiry using postMessage:

```javascript
// Refresh 5 minutes before expiry
setInterval(() => {
  const newToken = await fetchFreshTokenFromYourServer();
  frame.contentWindow.postMessage(
    { type: 'MSD_AUTH_TOKEN', token: newToken },
    'https://dashboard.yourdomain.com'
  );
}, 55 * 60 * 1000); // every 55 minutes
```

---

## 7. Moving to SQL Server

The server uses SQLite now. Migrating to SQL Server is a one-line change:

**Current (SQLite):**
```python
con = sqlite3.connect(DB_PATH)
```

**SQL Server (pyodbc):**
```python
import pyodbc
conn_str = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER={os.environ['SQL_SERVER']};"
    f"DATABASE={os.environ['SQL_DATABASE']};"
    "Authentication=ActiveDirectoryMsi;"  # or SQL auth
)
con = pyodbc.connect(conn_str)
```

All queries are standard ANSI SQL — no changes needed to the query logic.

---

## 8. API Reference

All endpoints require `Authorization: Bearer <token>` header.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/auth/verify` | Token validity check |
| GET | `/api/data/events` | All events with stats |
| GET | `/api/data/events/{event}/titles` | Titles for an event |
| GET | `/api/data/events/{event}/titles/{title}/users` | Users for event+title |
| GET | `/api/data/events/{event}/titles/{title}/users/{email}/actions` | Actions for a user |
| GET | `/api/data/titles` | All titles rolled up across events |
| GET | `/api/data/titles/{title}/events` | Events a title appears in |
| GET | `/api/data/actions` | All distinct action types |
| GET | `/api/data/chart` | Chart data (top 50 titles × actions) |
| POST | `/api/chat` | AI chat (Claude) |
