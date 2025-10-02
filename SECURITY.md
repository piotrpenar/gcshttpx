# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Security Features

### Explicit Credential Handling

**gcshttpx** prioritizes security through explicit credential management:

#### ✅ What We Do

- **No automatic filesystem searches**: Credentials must be explicitly provided via:
  - Direct file path to `service_file` parameter
  - File-like object (e.g., `io.StringIO`)
  - `GOOGLE_APPLICATION_CREDENTIALS` environment variable (only when `service_file=None`)

- **HTTPS-only enforcement**: All token endpoints must use HTTPS protocol

- **Input validation**:
  - Service account JSON structure validation
  - Private key format verification (must be PEM-encoded)
  - Required field validation (client_email, private_key, etc.)
  - Token refresh timing validation

- **No sensitive data in logs**: Errors never expose credentials or tokens

#### ❌ What We Don't Do

- **No automatic directory scanning**: We never search `~/.config/gcloud`, `%APPDATA%`, or system directories
- **No implicit defaults**: Empty credentials result in GCE metadata usage, never silent failures
- **No HTTP fallback**: Token endpoints must use HTTPS

### Authentication Best Practices

#### Service Account Keys

```python
from gcs_httpx import Token

# ✅ GOOD: Explicit path
token = Token(
    service_file="/secure/path/to/service-account.json",
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)

# ✅ GOOD: Environment variable (explicit)
# export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
token = Token(scopes=["https://www.googleapis.com/auth/cloud-platform"])

# ✅ GOOD: In-memory credentials (no disk access)
import io
import json
credentials = json.dumps(service_account_json)
token = Token(
    service_file=io.StringIO(credentials),
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)

# ❌ BAD: Don't commit credentials to version control
# ❌ BAD: Don't store credentials in application code
```

#### GCE Metadata Service

```python
# ✅ GOOD: Use GCE metadata when running on GCP
# This is safe as it requires network access to metadata.google.internal
token = Token(scopes=["https://www.googleapis.com/auth/cloud-platform"])
```

### Credential Storage

**Recommendations:**

1. **Development**:
   - Use `GOOGLE_APPLICATION_CREDENTIALS` environment variable
   - Store keys outside project directory
   - Add `*.json` to `.gitignore`

2. **Production**:
   - Use GCE/GKE metadata service (preferred)
   - Use secret management (GCP Secret Manager, Vault, etc.)
   - Use Workload Identity (GKE)
   - Never commit service account keys to repositories

3. **CI/CD**:
   - Use GitHub Secrets or equivalent
   - Inject credentials at runtime
   - Rotate keys regularly

### HTTP/2 Security

gcshttpx uses HTTP/2 by default, which provides:

- TLS 1.2+ requirement
- Binary framing (more secure parsing)
- Connection multiplexing (reduced overhead)

### Validation Errors

The library will raise `ValueError` for:

- Missing required credential fields
- Invalid private key format
- Non-HTTPS token URIs
- Invalid credential types
- Malformed service account JSON

**Example:**

```python
# This will raise ValueError
token = Token(service_file=io.StringIO('{"type": "service_account"}'))
# ValueError: Invalid service_account credentials: missing client_email, private_key
```

## Reporting a Vulnerability

If you discover a security vulnerability, please **DO NOT** open a public issue.

Instead, please report it privately:

1. **Email**: Create a security advisory on GitHub
2. **Response time**: We aim to respond within 48 hours
3. **Disclosure**: We follow coordinated disclosure practices

Please include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Security Updates

Security updates will be:

- Released as patch versions (e.g., 0.1.1)
- Documented in CHANGELOG.md
- Announced in release notes
- Tagged with `security` label

## Dependencies

We minimize dependencies for security:

- `httpx[http2]` - Well-maintained HTTP client
- `orjson` - Fast, secure JSON parser
- `PyJWT` - Industry-standard JWT library
- `cryptography` - Trusted cryptographic library

All dependencies are pinned to minimum versions and regularly updated.

## Security Checklist for Users

- [ ] Never commit service account keys to version control
- [ ] Use environment variables or secret managers for credentials
- [ ] Rotate service account keys regularly (90 days recommended)
- [ ] Use least-privilege scopes (not always `cloud-platform`)
- [ ] Enable GCP audit logging for storage access
- [ ] Use VPC Service Controls for additional protection
- [ ] Keep gcshttpx and dependencies updated
- [ ] Review GCP IAM permissions regularly

## Related Security Resources

- [GCP Service Account Best Practices](https://cloud.google.com/iam/docs/best-practices-service-accounts)
- [GCP Security Best Practices](https://cloud.google.com/security/best-practices)
- [Google Cloud Storage Security](https://cloud.google.com/storage/docs/best-practices)
