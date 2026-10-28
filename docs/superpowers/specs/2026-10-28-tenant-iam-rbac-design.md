# Tenant IAM and RBAC with external identity providers

**Date:** 2026-10-28
**Status:** Draft for approval
**Scope:** ai-service, recruiter/admin client, migrations, packaging, docs
**Chosen scope:** OIDC, SAML 2.0, and SCIM 2.0

## Intent

Let each tenant use its own identity provider for workforce identity, while
keeping interview data and workflow data isolated by tenant. Tenant admins must
be able to configure their provider, map provider groups to fixed platform
roles, manage users and sessions, issue service credentials, and review audit
events without gaining access to another tenant.

The current system has static API-key principals, no user accounts, no browser
login, and one `Principal.subject` value that serves as both actor and tenant.
This design separates those concepts and adds enterprise federation and
lifecycle management.

All three protocols are in scope:

- OIDC for browser SSO and modern IdPs such as Okta, Entra ID, Google,
  Auth0, and Keycloak.
- SAML 2.0 for IdPs that require it.
- SCIM 2.0 so an IdP can provision and deprovision users and groups.

Service-to-service traffic, including the gateway and channel adapters, keeps
using API keys. Browser users use short-lived server-side sessions, not tokens
in browser storage.

## Goals

- Add a first-class tenant record while preserving existing `owner_subject`
  data as tenant identifiers.
- Separate `Principal.tenant_id` (authorization boundary) from
  `Principal.subject` (audit actor).
- Let platform admins create tenants and bootstrap them.
- Let each tenant admin register one or more OIDC or SAML providers.
- Support IdP-initiated JIT user creation from signed OIDC/SAML assertions.
- Map IdP groups to a fixed role catalog.
- Support SCIM 2.0 Users and Groups for provisioning, updates, and immediate
  deprovisioning.
- Store IdP secrets encrypted at rest and never return them through the API.
- Use HttpOnly, Secure cookies for browser sessions, with CSRF protection.
- Add tenant-managed API keys for service accounts.
- Enforce permissions in addition to tenant ownership.
- Audit authentication, IAM, SCIM, role, key, and session events.
- Keep cross-tenant resource access indistinguishable from missing resources
  (`404`).
- Preserve loopback development, static environment principals, and the legacy
  key's non-tenant utility role.
- Keep the base PyPI install free of native SAML dependencies.

## Non-goals

- Local passwords, password reset, or password storage.
- Custom role creation or a permission editor. Roles and permissions are fixed.
- Outbound SCIM provisioning (this service is a SCIM server, not a client).
- SCIM Bulk, custom schema extensions, or full RFC filter grammar.
- SAML Single Logout, SAML IdP-initiated unsolicited responses, or OIDC
  back-channel logout.
- Email-domain tenant discovery or DNS domain verification. Users reach a tenant
  through its login URL.
- Nested IdP group expansion. Providers must emit effective groups or SCIM
  membership.
- Per-tenant databases or per-tenant encryption of interview content.
- Billing, invitations, or tenant self-service signup.
- Changing candidate interview authentication through the gateway.
- Replacing the gateway's single-tenant service-key model.

## Anti-goals

- Storing access, ID, refresh, or SCIM tokens in `localStorage`.
- Trusting unsigned SAML assertions or unverified OIDC signatures.
- Letting a tenant header, request body, or query parameter select the tenant
  for browser authentication.
- Mapping `platform_admin` or `admin` from an external IdP claim.
- Returning `403` for a cross-tenant object where `404` avoids disclosure.
- Logging or auditing client secrets, API keys, SCIM tokens, session cookies,
  raw claims, transcripts, or evidence.
- Allowing wildcard CORS together with credentialed cookies.
- Falling back to a weaker authentication path when an IdP is misconfigured.
- Encoding authorization decisions in the frontend only.

## Approaches considered

### 1. Browser-held OIDC tokens

The SPA performs OIDC authorization-code + PKCE and sends access tokens as
bearer credentials.

- Pros: no login routes in the API, no cookie/CSRF handling.
- Cons: tokens live in the browser, refresh is fragile, SAML cannot share the
  flow, and service/API authorization still needs a separate path.
- Rejected.

### 2. Backend-for-frontend with server-side sessions (chosen)

The ai-service performs the OIDC/SAML code exchange or assertion validation and
creates an HttpOnly session cookie. The SPA only calls same-origin APIs with
`credentials: include` and a CSRF token.

- Pros: client secrets and IdP tokens stay server-side; OIDC and SAML share one
  session model; SCIM, admin revocation, and role changes take effect on the
  next request; no new frontend OIDC dependency.
- Cons: requires cookie, CORS, CSRF, and same-origin deployment discipline.
- Chosen.

### 3. External authentication proxy

Deploy oauth2-proxy or a SAML proxy in front of the API.

- Pros: offloads federation and cookie handling.
- Cons: cannot provide per-tenant self-service configuration, tenant-scoped
  admin APIs, SCIM, or integrated role mapping without duplicating state.
- Rejected for this product's tenant-admin requirement.

## Architecture

```
Browser (recruiter/admin)
  -> TLS reverse proxy / same origin
  -> FastAPI auth routes (OIDC, SAML, session cookie, CSRF)
  -> FastAPI tenant APIs (permission + tenant checks)
  -> SQLite

IdP (Okta/Entra/Google/Keycloak)
  -> OIDC redirect or SAML POST -> FastAPI auth routes
  -> SCIM HTTPS + tenant/provider bearer token -> FastAPI /scim/v2

Gateway/channel adapters
  -> tenant service API key -> FastAPI tenant APIs
```

### Deployment assumption

Production should serve the SPA and API from the same site through one TLS
reverse proxy. `PYINTERVIEWBOT_PUBLIC_BASE_URL` is required for federation and
is used for OIDC redirect URIs and SAML entity/ACS URLs. Cross-site SPA/API
deployment is supported only with `SameSite=None`, `Secure` cookies, explicit
CORS origins, and CSRF tokens.

## Tenant and principal model

### Tenant

A tenant is the authorization boundary. Existing `owner_subject`,
`tenant_subject`, and workflow owner values are tenant IDs. The column names
stay for migration compatibility; new code treats them as `tenant_id` values.

New table:

```text
tenants(
  id TEXT PRIMARY KEY,
  slug TEXT UNIQUE,            -- lowercase, URL-safe, nullable for legacy rows
  name TEXT NOT NULL,
  status TEXT NOT NULL,        -- active | disabled
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
```

- Existing distinct owner subjects are backfilled as tenant IDs with no slug.
- New tenants use UUID strings.
- A tenant is disabled by a platform admin; all sessions and keys stop working.
- Login URL shape: `/login/{tenant_slug}` and
  `/login/{tenant_slug}/{provider_slug}`.

### Principal

`Principal` becomes:

```text
tenant_id: str
subject: str                 -- stable actor subject for audit
roles: frozenset[str]
permissions: frozenset[str]
source: str                  -- session | env_principal | tenant_api_key |
                                platform_session | legacy | loopback
provider_id: str | None
session_id: str | None
```

- `tenant_id` is used for every ownership check.
- `subject` is used as the audit actor.
- Existing environment principals set `tenant_id = subject` and
  `subject = subject`.
- Browser sessions set `tenant_id` from the user record and `subject` to the
  local user ID.
- `platform_admin` and the legacy `admin` role may cross tenant boundaries.
  `tenant_admin` may not.
- `require_tenant_auth` continues to reject the legacy environment key. It
  accepts environment principals, tenant API keys, browser sessions, and
  loopback identity. A platform-key browser session is admin-only and is not a
  tenant principal; tenant routes reject it.

### Authentication precedence

For an API request:

1. `X-API-Key` or `Authorization: Bearer <api-key>`.
2. Tenant browser session cookie.
3. Platform browser session cookie for platform admin routes.
4. Loopback fallback only when no authentication is configured.

An explicit API key wins over a cookie. A request authenticated by cookie is
subject to CSRF checks; a request authenticated by API key is not.

## Roles and permissions

The role catalog is fixed. Tenants map groups to these roles; they do not
define new roles.

| Role | Purpose |
|---|---|
| `platform_admin` | Cross-tenant platform operation. Static environment only. |
| `admin` | Legacy alias for `platform_admin`. Static environment only. |
| `owner` | Full tenant administration, including IAM. Legacy-compatible. |
| `tenant_admin` | Full tenant administration, including IAM. |
| `recruiter` | Create/read/delete tenant interviews, read/export reports, review evidence. |
| `auditor` | Read-only interview, report, workflow, and audit access. |
| `author` | Create and edit workflows. |
| `approver` | Approve workflow versions. |
| `deployer` | Deploy and roll back approved workflow versions. |
| `service` | Service-account access for interviews, turns, resumes, channels, and workflow reads. API-key only. |

Permission catalog:

```text
sessions.read, sessions.create, sessions.write, sessions.delete
reports.read, reports.export, reviews.write
workflows.read, workflows.author, workflows.approve, workflows.deploy
channels.manage, compliance.manage
iam.manage, audit.read
```

Role grants:

- `owner`, `tenant_admin`: all tenant-scoped permissions.
- `recruiter`: `sessions.read`, `sessions.create`, `sessions.delete`,
  `reports.read`, `reports.export`, `reviews.write`.
- `auditor`: `sessions.read`, `reports.read`, `workflows.read`, `audit.read`.
- `author`: `workflows.read`, `workflows.author`.
- `approver`: `workflows.read`, `workflows.approve`.
- `deployer`: `workflows.read`, `workflows.deploy`.
- `service`: `sessions.read`, `sessions.create`, `sessions.write`,
  `sessions.delete`, `channels.manage`, `workflows.read`.
- `platform_admin`/`admin`: all permissions across all tenants, plus platform
  tenant administration.

Rules:

- IdP group mappings may grant only `owner`, `tenant_admin`, `recruiter`,
  `auditor`, `author`, `approver`, or `deployer`.
- `platform_admin`, `admin`, and `service` cannot be granted from OIDC/SAML
  claims or SCIM groups.
- Tenant API keys may carry `service`, `recruiter`, `auditor`, `author`,
  `approver`, or `deployer`. They cannot carry `owner`, `tenant_admin`,
  `platform_admin`, or `admin`, so a leaked service key cannot administer IAM.
- Manual role grants may carry the same roles as IdP group mappings. They
  cannot grant `service`, `platform_admin`, or `admin`.
- Permissions are recomputed from active user roles and current group
  memberships on every request. There is no role cache in the cookie.
- Tenant ownership and permission checks are separate. Both must pass.

## Data model

All IAM tables carry `tenant_id`. Foreign keys cascade within a tenant.

### `tenants`

Defined above.

### `users`

```text
users(
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  email TEXT NOT NULL,             -- normalized lowercase
  display_name TEXT NOT NULL,
  active INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, email)
)
```

### `user_identities`

```text
user_identities(
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider_id TEXT NOT NULL REFERENCES idp_configs(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,              -- oidc_sub | saml_nameid | scim_external_id |
                                    -- scim_user_name
  external_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(provider_id, kind, external_id)
)
```

A user may have identities from more than one provider. `kind` distinguishes
the identity namespace:

- OIDC: `oidc_sub`, value is the ID-token `sub`.
- SAML: `saml_nameid`, value is the validated NameID.
- SCIM: `scim_external_id` when present, otherwise `scim_user_name`.

Resolution order on login or SCIM create is: exact
`(provider_id, kind, external_id)`; then an active user in the same tenant with
the same normalized email; then JIT creation. An exact identity always wins.
If the provider supplies a different email, the email is updated only when it
is unique in the tenant and passes the provider's domain policy. If the email
belongs to another user, provisioning/login fails with `409` (SCIM) or a
generic authentication failure (browser) rather than merging accounts.

SCIM may therefore attach to a JIT-created user, and JIT may attach to a
SCIM-created user, but only through the trusted provider and email policy.
Email changes never re-link an identity to a different user.

### `iam_groups`

```text
iam_groups(
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  provider_id TEXT NOT NULL REFERENCES idp_configs(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider_id, external_id)
)
```

Group claims and SCIM groups upsert these rows. Matching accepts either
`external_id` or `display_name` so providers that emit names and providers that
emit IDs both work.

### `iam_group_members`

```text
iam_group_members(
  group_id TEXT NOT NULL REFERENCES iam_groups(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source TEXT NOT NULL,            -- oidc | saml | scim
  updated_at TEXT NOT NULL,
  PRIMARY KEY(group_id, user_id, source)
)
```

Membership authority is per provider:

- If a provider has an active SCIM token, SCIM is authoritative for that
  provider's group memberships. Effective role resolution ignores
  `oidc`/`saml` membership rows for that provider, and login does not rewrite
  SCIM-managed memberships.
- If a provider has no active SCIM token, login claims replace that user's
  `oidc`/`saml` memberships for the provider. An absent or empty group claim
  clears them.
- Creating the first active SCIM token does not silently delete data, but the
  precedence rule means stale login-sourced memberships stop affecting
  permissions immediately. Revoking the last SCIM token returns authority to
  login claims at the next login.
- SCIM group updates replace SCIM-sourced membership rows for that group.

### `iam_group_roles`

```text
iam_group_roles(
  group_id TEXT NOT NULL REFERENCES iam_groups(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  granted_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(group_id, role)
)
```

Admins can create a placeholder group and mapping before the first login by
entering the claim value as both ID and display name.

### `user_roles`

Manual role grants that are independent of group membership:

```text
user_roles(
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  granted_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(user_id, role)
)
```

Effective roles are the union of `user_roles` and roles mapped from the user's
current group memberships, excluding disallowed roles. If the union is empty,
the user can authenticate but has no data permissions.

### `idp_configs`

```text
idp_configs(
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,              -- oidc | saml
  slug TEXT NOT NULL,              -- URL-safe, unique within tenant
  display_name TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  is_default INTEGER NOT NULL,
  public_config_json TEXT NOT NULL,
  secret_config_encrypted TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, slug)
)
```

`public_config_json` contains non-secret settings:

- OIDC: issuer, discovery URL, client ID, scopes, group claim path, email
  claim, name claim, allowed email domains.
- SAML: IdP entity ID, SSO URL, IdP signing certificate, metadata source,
  attribute names for email/name/groups, allowed email domains, SP certificate.

`secret_config_encrypted` contains the OIDC client secret and the SAML SP
private key. It is encrypted with the current key from
`PYINTERVIEWBOT_IAM_SECRET_KEYS` using Fernet. Secrets are never returned; admin
views expose only `has_secret`, certificate fingerprints, and timestamps.

A tenant may have multiple providers. Exactly one enabled provider is default.
`/auth/login/{tenant_slug}` uses the default; explicit provider URLs override it.

### `platform_sessions`

Browser sessions created from a platform API key are separate from tenant
sessions and cannot reach tenant interview APIs:

```text
platform_sessions(
  id TEXT PRIMARY KEY,             -- hash of the cookie token
  actor_subject TEXT NOT NULL,     -- platform key prefix, never the secret
  csrf_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  ip_hash TEXT,
  user_agent_hash TEXT
)
```

The platform session cookie is `pyib_platform_session`. It authenticates only
`/admin/tenants*` platform operations and `/auth/me`. It cannot list interviews,
start sessions, read transcripts, or impersonate a tenant user. Break-glass
tenant API access remains available by using a platform API key directly, with
existing cross-tenant audit behavior.

### `user_sessions`

```text
user_sessions(
  id TEXT PRIMARY KEY,             -- hash of the cookie token
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider_id TEXT NOT NULL,
  csrf_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  ip_hash TEXT,
  user_agent_hash TEXT
)
```

- Cookie token: 32 random bytes, base64url, stored only as SHA-256.
- Default absolute lifetime: 12 hours.
- Default idle timeout: 30 minutes.
- Updating `last_seen_at` is throttled to at most once per minute.
- Deleting or disabling a user revokes all sessions in the same transaction.
- Revocation and expiry are checked on every request.

### `auth_transactions`

Short-lived OIDC/SAML login state:

```text
auth_transactions(
  id TEXT PRIMARY KEY,             -- hash of state/transaction cookie
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  protocol TEXT NOT NULL,
  state_hash TEXT NOT NULL,
  nonce_hash TEXT,
  pkce_verifier_encrypted TEXT,
  oidc_nonce_hash TEXT,
  saml_request_id TEXT,
  return_to TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  consumed_at TEXT
)
```

Rows expire after 10 minutes and are consumed once. `return_to` must be a
relative path under `/recruiter` or `/admin`.

### `saml_replay`

```text
saml_replay(
  assertion_id_hash TEXT PRIMARY KEY,
  expires_at TEXT NOT NULL
)
```

Expired rows are removed opportunistically.

### `tenant_api_keys`

```text
tenant_api_keys(
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  name TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  roles_json TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  expires_at TEXT,
  revoked_at TEXT
)
```

- Secret format: `pyib_<prefix>_<secret>`.
- Only the hash and prefix are stored.
- The full key is shown once at creation.
- Keys are tenant-bound; no platform admin role.
- Revocation takes effect on the next request.

### `scim_tokens`

```text
scim_tokens(
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  provider_id TEXT NOT NULL REFERENCES idp_configs(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  token_prefix TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  expires_at TEXT,
  revoked_at TEXT
)
```

SCIM requests use `Authorization: Bearer <token>` against `/scim/v2`. The token
identifies both the tenant and the provider. SCIM tokens are shown once, hashed,
revocable, and audited.

## Authentication flows

### OIDC login

1. Browser opens `/login/{tenant_slug}` or a provider-specific login URL.
2. `GET /auth/tenants/{slug}` returns enabled provider display metadata only.
3. `GET /auth/login/{tenant_slug}/{provider_slug}?return_to=...`:
   - discovers the issuer and validates exact issuer match over HTTPS
     (HTTP is allowed only for a loopback test issuer);
   - creates a one-time transaction with state, nonce, and PKCE verifier;
   - redirects to the IdP authorization endpoint.
4. The IdP redirects to `/auth/oidc/callback`.
5. The callback validates state, nonce, code, ID-token signature, issuer,
   audience, and expiry using the provider's JWKS.
6. The user is resolved by exact identity `(provider_id, oidc_sub, sub)`. If
   absent, JIT creation requires an email in an allowed domain and links by
   email only when a matching active tenant user exists. An explicit
   `email_verified=false` is rejected; an absent claim is treated as
   IdP-asserted because the provider was configured by a tenant admin.
7. Group claims are synchronized, roles are derived, and a local session is
   created.
8. The response sets the HttpOnly session cookie and redirects to the validated
   relative `return_to`.

The client secret is used only server-side. OIDC discovery and JWKS responses
are size-limited, time-limited, and cached briefly. Implicit and hybrid flows
are not supported.

### SAML login

1. `GET /auth/login/{tenant_slug}/{provider_slug}` loads provider metadata,
   generates a signed AuthnRequest, stores its ID in `auth_transactions`, and
   redirects to the IdP SSO URL.
2. The IdP posts a SAML response to `/auth/saml/acs/{provider_id}`.
3. The response is validated for signature, audience, recipient, destination,
   time window, `InResponseTo`, and replay. Unsigned assertions are rejected.
   SHA-1 is rejected. Clock skew defaults to 120 seconds.
4. The NameID is the external identity. Email, display name, and groups come
   from configurable attributes; email is required and must be in an allowed
   domain.
5. JIT user creation, group synchronization, session creation, and redirect use
   the same path as OIDC.
6. `GET /auth/saml/metadata/{provider_id}` exposes the tenant provider's SP
   metadata and signing certificate.
7. IdP-initiated unsolicited responses and SAML Single Logout are rejected.

Each SAML provider gets its own generated RSA-2048 SP keypair. The private key
is encrypted at rest; the certificate is public in metadata. Key rotation is an
explicit admin action that changes the metadata the IdP must trust.

SAML support is an optional install extra:

```toml
[project.optional-dependencies]
saml = ["python3-saml>=1.16,<2"]
```

If a SAML provider is enabled without the extra, startup/configuration fails
closed with a clear installation error. The base `pyinterviewbot-ai` install
stays free of `xmlsec` native dependencies.

### Browser session

- Cookie name: `pyib_session`.
- `HttpOnly`, `Path=/`, `Secure` when the public base URL is HTTPS.
- `SameSite=Lax` for same-site deployments; configurable to `None` for
  cross-site deployments.
- CSRF token returned by `GET /auth/me`, stored only in memory by the SPA, and
  required in `X-CSRF-Token` on unsafe cookie-authenticated requests.
- Same-origin/`Origin` checks supplement CSRF tokens.
- `POST /auth/logout` revokes the session row, clears the cookie, and audits the
  event. IdP global logout is not attempted in this scope.
- `GET /auth/me` returns tenant, provider, user, roles, permissions, and CSRF
  token. It never returns IdP tokens or secrets.

### Platform bootstrap

- Platform admins are static environment principals with `platform_admin` or
  the legacy `admin` role.
- They create tenants and can configure the first provider.
- For the admin SPA, a platform key can be exchanged at
  `POST /auth/platform-session` for an HttpOnly platform session. The key is
  never stored in the browser; the route is rate-limited and audited. The
  platform session is admin-only.
- A tenant admin is established either by mapping an IdP group to
  `tenant_admin` before first login or by a platform admin assigning a manual
  role after JIT creation.
- Tenant metadata (name and slug) may be updated by a platform admin or by an
  `owner`/`tenant_admin` of that tenant. Tenant status may be changed only by a
  platform admin.

Existing environment principals keep working as tenant principals. An
environment principal with `owner` or `tenant_admin` can configure IAM for its
own tenant through the API. It cannot cross tenants unless it has
`platform_admin`/`admin`.

On startup, after migrations, the service reconciles tenant rows for
`local-loopback` and every subject configured in
`PYINTERVIEWBOT_PRINCIPALS`. It uses `INSERT OR IGNORE`, never overwrites an
existing tenant, and leaves the slug null. This makes a fresh environment
principal usable for its own IAM setup without a platform bootstrap step.

## Email trust policy

- A browser login requires an email claim. SAML email is trusted from the
  signed assertion; OIDC email is trusted when `email_verified` is missing or
  true, and rejected when it is false.
- Every enabled provider must have at least one allowed email domain. Exact
  domains and `*.example.com` wildcards are supported. A provider with an empty
  list cannot be enabled.
- OIDC, SAML, and SCIM email values are normalized to lowercase and must match
  an allowed domain. SCIM create/update of a non-matching email returns `400`
  with a SCIM error; browser login fails generically.
- SCIM-created users and SCIM email changes obey the same policy as JIT users.
- Email uniqueness is enforced per tenant. A collision is never auto-merged.

## SCIM 2.0

### Endpoints

```text
GET    /scim/v2/ServiceProviderConfig
GET    /scim/v2/ResourceTypes
GET    /scim/v2/Schemas
GET    /scim/v2/Users
POST   /scim/v2/Users
GET    /scim/v2/Users/{id}
PUT    /scim/v2/Users/{id}
PATCH  /scim/v2/Users/{id}
DELETE /scim/v2/Users/{id}
GET    /scim/v2/Groups
POST   /scim/v2/Groups
GET    /scim/v2/Groups/{id}
PUT    /scim/v2/Groups/{id}
PATCH  /scim/v2/Groups/{id}
DELETE /scim/v2/Groups/{id}
```

- Content type: `application/scim+json` (JSON is also accepted).
- Authentication: tenant/provider SCIM bearer token.
- User create maps `userName` and primary email to the tenant user, creates the
  provider identity, and stores active state.
- User update/PATCH supports `active`, `name.*`, `displayName`, `emails`, and
  `userName`.
- `active=false` or DELETE disables the user, removes group memberships, and
  revokes all user sessions immediately.
- Group create/update/PATCH supports `displayName`, `externalId`, and `members`.
- Group membership changes update effective roles on the next request. The
  standard Okta add/remove member PATCH shapes are supported.
- Delete group removes memberships and mappings.
- List supports `startIndex`, `count`, and the filter subset:
  `userName eq`, `externalId eq`, `displayName eq`, `emails.value eq`.
- Unknown filters return `400 invalidFilter`.
- Responses include SCIM `schemas`, `id`, `meta`, `externalId`, and
  `totalResults` where applicable.
- SCIM errors use the standard `schemas`, `detail`, and `status` shape.

SCIM is disabled for a tenant until a token exists. Token creation and
revocation are tenant-admin actions and are audited.

## Admin API and console

All admin routes are tenant-scoped and return `404` when the caller is not a
platform admin and the tenant is not theirs.

```text
GET    /admin/tenants                         platform admin
POST   /admin/tenants                         platform admin
GET    /admin/tenants/{tenant_id}             tenant or platform admin
PATCH  /admin/tenants/{tenant_id}             name/slug: tenant or platform admin;
                                              status: platform admin
GET    /admin/tenants/{tenant_id}/providers
POST   /admin/tenants/{tenant_id}/providers
PATCH  /admin/tenants/{tenant_id}/providers/{provider_id}
DELETE /admin/tenants/{tenant_id}/providers/{provider_id}
POST   /admin/tenants/{tenant_id}/providers/{provider_id}/test
POST   /admin/tenants/{tenant_id}/providers/{provider_id}/rotate-saml-key
GET    /admin/tenants/{tenant_id}/groups
POST   /admin/tenants/{tenant_id}/groups
POST   /admin/tenants/{tenant_id}/groups/{group_id}/roles
DELETE /admin/tenants/{tenant_id}/groups/{group_id}/roles/{role}
GET    /admin/tenants/{tenant_id}/users
PATCH  /admin/tenants/{tenant_id}/users/{user_id}
PUT    /admin/tenants/{tenant_id}/users/{user_id}/roles
POST   /admin/tenants/{tenant_id}/users/{user_id}/revoke-sessions
GET    /admin/tenants/{tenant_id}/sessions
POST   /admin/tenants/{tenant_id}/sessions/{session_id}/revoke
GET    /admin/tenants/{tenant_id}/api-keys
POST   /admin/tenants/{tenant_id}/api-keys
POST   /admin/tenants/{tenant_id}/api-keys/{key_id}/revoke
GET    /admin/tenants/{tenant_id}/scim-tokens
POST   /admin/tenants/{tenant_id}/scim-tokens
POST   /admin/tenants/{tenant_id}/scim-tokens/{token_id}/revoke
GET    /admin/tenants/{tenant_id}/audit
```

`POST /groups` creates a placeholder or explicit group with `provider_id`,
`external_id`, `display_name`, and optional initial roles. It upserts by exact
`external_id`, then by exact `display_name`; if those match different groups it
returns `409` for the admin to resolve. This lets an admin map an Okta group
before any user logs in. `POST /groups/{group_id}/roles` uses the returned local
group ID.

Provider lifecycle:

- Disabling a provider blocks new logins, revokes that provider's browser
  sessions, and revokes its SCIM tokens. Users, identity links, groups, and
  role mappings remain.
- Deleting a provider revokes its sessions and SCIM tokens and cascades its
  identity/group data. Users with no remaining identity are marked inactive;
  manual roles remain for audit.
- Changing the default provider affects only the tenant login entry point.
- Disabling a tenant revokes all browser sessions, API keys, and SCIM tokens,
  and blocks all tenant principals. A platform admin can still read and
  re-enable the tenant.

The admin console is a new `/admin` route in the existing Vite client. It
provides:

- Provider setup for OIDC and SAML, including callback/ACS/metadata values,
  connection testing, allowed domains, and group claim/attribute names.
- Group-to-role mapping.
- User list, disable/enable, manual role assignment, and session revocation.
- Tenant service API keys.
- SCIM base URL and one-time token creation.
- Audit event list.
- Platform tenant creation for platform admins.

The console uses the same cookie session as `/recruiter`; it does not store
credentials. Secret fields are write-only. The UI remains plain TypeScript and
matches the existing minimal client style.

The recruiter view stops prompting for an API key when a session cookie is
present. A manual-key fallback remains behind an explicit local-development
flag. A `401` redirects to `/login` rather than exposing a credential prompt by
default.

## Enforcement changes

- `Principal.subject` no longer represents the tenant. All ownership checks,
  list filters, channel scopes, compliance scopes, and audit tenant values use
  `Principal.tenant_id`.
- `authorized_session`, session listing, channel start, compliance policy, and
  workflow ownership compare against `tenant_id`.
- Route permission checks use the permission catalog in addition to tenant
  ownership:
  - session read/create/write/delete;
  - report read/export;
  - evidence review write;
  - workflow read/author/approve/deploy;
  - channel and compliance management;
  - IAM and audit administration.
- Auxiliary routes are mapped as follows: `/templates` and `/providers` remain
  `require_auth` catalog reads; `/documents/parse` requires
  `sessions.create`; `/asr/transcribe` and `/tts/synthesize` require
  `sessions.write`. The `service` role therefore drives the gateway without
  granting browser users expensive speech access.
- Cross-tenant object IDs return `404`. Missing permission on an owned resource
  returns `403`.
- Platform admins retain cross-tenant access through the existing admin
  behavior, now named `platform_admin` with `admin` as a compatibility alias.
- Tenant admins cannot cross tenants.
- Audit writes record `actor_subject = Principal.subject` and
  `tenant_subject = resource tenant` or `Principal.tenant_id`.

## Migrations

Add migration `005_iam`:

1. Create all IAM tables and indices.
2. Insert tenant rows for every distinct non-empty tenant/owner subject found in
   sessions, workflows, channel rows, compliance policy, and audit events.
3. Leave legacy tenant slugs null until a platform admin or the tenant's
   owner/tenant_admin assigns them.
4. Do not rewrite existing owner columns. They remain tenant identifiers.
5. Do not create user rows for historical actor subjects.
6. Preserve the legacy key and loopback behavior.

The migration is additive and idempotent. Existing sessions, workflows, channel
mappings, and compliance policies keep working under their existing tenant IDs.

## Audit events

Add actions without secrets or content:

```text
auth.login, auth.login_failed, auth.logout, auth.session_revoked
iam.tenant_created, iam.tenant_updated
iam.provider_created, iam.provider_updated, iam.provider_deleted
iam.provider_tested, iam.saml_key_rotated
iam.role_mapping_added, iam.role_mapping_removed
iam.user_disabled, iam.user_enabled, iam.user_roles_changed
iam.api_key_created, iam.api_key_revoked
iam.scim_token_created, iam.scim_token_revoked
scim.user_created, scim.user_updated, scim.user_disabled, scim.user_deleted
scim.group_created, scim.group_updated, scim.group_deleted
```

Details may include tenant ID, provider ID, provider kind, group ID, target user
ID, role names, outcome, and a fixed failure reason. They must not include
tokens, secrets, cookies, raw claims, email addresses, transcripts, evidence, or
request bodies.

## Configuration

New environment variables:

```text
PYINTERVIEWBOT_PUBLIC_BASE_URL          required for federation; e.g. https://interviews.example.com
PYINTERVIEWBOT_IAM_SECRET_KEYS          comma-separated Fernet keys; first is current
PYINTERVIEWBOT_COOKIE_SECURE            default true when public URL is HTTPS
PYINTERVIEWBOT_COOKIE_SAMESITE          lax (default) or none
PYINTERVIEWBOT_SESSION_HOURS            default 12
PYINTERVIEWBOT_SESSION_IDLE_MINUTES     default 30
PYINTERVIEWBOT_OIDC_HTTP_TIMEOUT        default 10 seconds
PYINTERVIEWBOT_SAML_CLOCK_SKEW_SECONDS  default 120
PYINTERVIEWBOT_TRUSTED_PROXY_HEADERS    opt-in; default off
```

- If encrypted IAM secrets exist and no usable key is configured, startup fails
  closed.
- The first key encrypts new secrets; older keys decrypt existing values.
- A documented maintenance command re-encrypts stored secrets to the current
  key.
- `PYINTERVIEWBOT_PUBLIC_BASE_URL` must be HTTPS outside loopback development.
- Static `PYINTERVIEWBOT_PRINCIPALS`, `PYINTERVIEWBOT_API_KEY`,
  `PYINTERVIEWBOT_LEGACY_SESSION_OWNER`, and loopback behavior remain supported.

## Security properties

- Tenant isolation is enforced by `tenant_id`, not user email, slug, or request
  input.
- OIDC: authorization-code + PKCE, state, nonce, exact issuer, audience,
  signature, expiry, allowed-domain, and `email_verified=false` checks.
- SAML: signed assertion, audience, recipient, destination, time, request-ID,
  SHA-256, and replay checks.
- SAML metadata URLs are HTTPS-only, size-limited, time-limited, and
  SSRF-guarded; XML paste is also supported.
- SCIM and API tokens are hashed, prefixed, scoped, revocable, and shown once.
- Secrets are encrypted at rest and write-only in the API/UI.
- Browser sessions are HttpOnly, Secure, SameSite-scoped, CSRF-protected, and
  revocable.
- Login and token endpoints are rate-limited and audited.
- Secrets and tokens never enter logs, audit details, local storage, or error
  responses.
- Role changes and deprovisioning take effect on the next request.
- Platform administration is reserved to static credentials; external IdPs
  cannot mint platform admins.

## Compatibility and breaking changes

- Internal `Principal` construction changes from one subject field to
  `tenant_id` plus actor `subject`. All internal call sites and tests change.
- Session routes gain permission checks. The default environment principal
  roles include `owner`, so existing default keys keep full tenant access.
  Custom keys with only `author`/`approver`/`deployer` lose session access
  unless granted `service`, `recruiter`, `owner`, or `tenant_admin`.
- Browser recruiter access uses SSO cookies instead of the API-key prompt.
  Local API-key fallback requires explicit opt-in.
- IdP setup requires `PYINTERVIEWBOT_PUBLIC_BASE_URL` and, when secrets are
  stored, `PYINTERVIEWBOT_IAM_SECRET_KEYS`.
- Deployments enabling SAML must install the `saml` extra.
- Existing tenants must be assigned a slug and provider by a platform admin or
  their own owner/tenant_admin before their users can use SSO.
- The legacy `admin` role remains a cross-tenant platform role. It is not
  renamed in existing environment configuration.

## Testing

- Unit tests for permission resolution and role mapping.
- OIDC tests with a mock discovery/JWKS server: valid login, wrong issuer,
  wrong audience, bad signature, expired token, nonce/state replay, PKCE
  failure, unverified email, disallowed domain, and JIT linking.
- SAML tests with signed fixtures: valid login, bad signature, unsigned
  assertion, wrong audience/recipient, expired/not-yet-valid assertion,
  replayed assertion, wrong InResponseTo, SHA-1 rejection, and ACS flow.
- SCIM tests for Users/Groups create, update, PATCH, delete, active=false,
  filters, pagination, malformed payloads, token revocation, and immediate
  session revocation.
- Cookie/CSRF tests for safe and unsafe methods, cross-origin rejection, logout,
  idle expiry, absolute expiry, revoked users, and platform-session isolation.
- RBAC matrix tests for every role and protected route.
- Tenant isolation tests for providers, users, groups, sessions, API keys, SCIM
  tokens, and audit: tenant admin cross-tenant attempts return `404`.
- Migration tests from pre-IAM schemas with multiple owner subjects and startup
  reconciliation for environment principals and loopback.
- Admin UI typecheck/build and recruiter no-credential-storage assertions.
- Full `pytest`, `ruff`, client build, `bandit`, `pip-audit`, and docs update.
- Manual Okta (OIDC, SAML, SCIM), Entra OIDC, and Keycloak validation steps
  documented; live validation recorded separately.

## Acceptance criteria

- A platform admin creates a tenant and assigns a slug.
- A tenant admin configures an OIDC provider, maps an Okta group to
  `recruiter`, and a user in that group logs in through Okta and sees only that
  tenant's interviews.
- A second tenant configures a different provider and cannot see the first
  tenant's providers, users, sessions, API keys, SCIM tokens, or audit events.
- A SAML tenant completes SP-initiated login with a signed assertion; unsigned,
  replayed, wrong-audience, and expired assertions are rejected.
- SCIM creates a user and group, maps the group to `auditor`, and disabling the
  user in the IdP immediately revokes their browser sessions.
- With both SCIM and login group claims enabled, SCIM remains authoritative and
  a later login does not restore a role removed in SCIM.
- A tenant service key still starts and drives an interview through the gateway.
- Revoking an API key or SCIM token takes effect on the next request.
- No browser storage, logs, audit rows, or API responses contain IdP tokens,
  client secrets, API keys, or SCIM tokens.
- Cross-tenant access returns `404`; missing permission on an owned resource
  returns `403`.
- The base package installs without `xmlsec`; SAML is available through the
  `saml` extra.
- The full test suite, lint, client build, and security scans pass.

## Risks and open operational notes

- SAML is the largest security surface. It depends on a mature library and
  signed-fixture tests; the optional extra avoids imposing native dependencies
  on every install.
- Without SCIM, group changes take effect on the next login. Tenants that need
  immediate lifecycle changes should configure SCIM.
- Cross-site SPA/API deployment weakens cookie isolation. Same-origin
  deployment is the supported production default.
- Loss of every configured IAM master key makes stored IdP secrets
  unrecoverable; they must be re-entered and SAML keys rotated.
- Platform admins remain a cross-tenant break-glass role. Their actions are
  audited, but they are not constrained by tenant ownership.
- IdP outages block new logins but do not invalidate existing local sessions.
  Tenants can revoke sessions manually or through SCIM.
- Tenant slugs are public login identifiers. They reveal tenant existence, not
  tenant data.
- Creating the first SCIM token switches group-membership authority for that
  provider to SCIM. Admins must ensure SCIM groups and mappings are complete
  before enabling it.
- SAML SLO, OIDC back-channel logout, and IdP-initiated SAML are not included;
  local sessions can still be revoked centrally.
