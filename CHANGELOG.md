# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - 2024-10-02

Initial release of **gcshttpx** - a minimal, secure async Google Cloud Storage client built on httpx with native HTTP/2 support.

### Features

- **Authentication**:
  - Service account authentication with JWT signing
  - Authorized user authentication
  - GCE metadata server authentication
  - Explicit credential handling (no automatic filesystem searches)
  - HTTPS-only token endpoints
  - Comprehensive input validation

- **Storage Operations**:
  - Upload/download with streaming support
  - Resumable uploads for large files
  - List objects with pagination
  - Bucket operations
  - Object composition
  - Metadata operations
  - Signed URLs via IAM

- **Developer Experience**:
  - Full async/await support
  - Native HTTP/2 with httpx
  - Complete type hints (py.typed)
  - Python 3.10+ with modern syntax
  - Comprehensive documentation
  - 76% test coverage

- **Security**:
  - Explicit credential sources only
  - Private key validation (PEM format)
  - Required field validation
  - No sensitive data in error messages
  - Detailed security documentation


