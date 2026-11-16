# ADR 0105: TLS minimums

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 82-security.md, 84-testing.md
- **Related ADRs:** 0010, 0026, 0098, 0104, 0106, 0112, 0125, 0150

## Context

Every connection in this product is HTTPS or TLS-wrapped TDS, and the floor for each one must be set in the templates and checked before deployment. The services differ in what a template can enforce: some accept TLS 1.3 as the minimum, some negotiate 1.3 but can't refuse 1.2, and some expose no setting at all. Every client the product runs (Python 3.13 with OpenSSL 3, Microsoft ODBC Driver 18, current browsers) can speak TLS 1.3, so the floor can follow what each service allows.

Vendor facts:

- App Service accepts inbound TLS 1.0 to 1.3, and 1.3 "is fully supported and can be the minimum"; the default minimum for the site and the SCM site is 1.2; `siteConfig.minTlsVersion` and `siteConfig.scmMinTlsVersion` take `'1.0'` to `'1.3'`. TLS 1.3 offers only TLS_AES_256_GCM_SHA384 and TLS_AES_128_GCM_SHA256. `minTlsCipherSuite` needs Basic or higher; end-to-end TLS (`endToEndEncryptionEnabled`) exists on Premium plans and the older Standard plan only.
- Azure SQL Database: `minimalTlsVersion` takes `'1.0'`, `'1.1'`, `'1.2'`, `'1.3'` or `'None'`; 1.3 "can be enforced but may break clients without it"; an enforced minimum can't be reverted to the default; a client below the minimum gets "Login failed with invalid TLS version". TDS 7.x carries TLS up to 1.2; TDS 8.0 (`Encrypt=strict`) starts TLS before prelogin and supports 1.3, needs ODBC Driver 18.1.2.1 or later (or ADO.NET 5.1, JDBC 11.2, OLE DB 19.2, mssql-python) and refuses `TrustServerCertificate=true`.
- Azure Storage lists `TLS1_3` in its enum, but "Minimum TLS version 1.3 version is not supported"; with no value set it accepts TLS 1.0 and above; a request below the minimum gets HTTP 400.
- Cosmos DB accepts only `Tls12` as a setting; TLS 1.0 and 1.1 were retired on 31 August 2025 and TLS 1.3 has been negotiated since 31 March 2025.
- Key Vault supports TLS 1.2 and 1.3; AI Search enforces 1.2 or 1.3 and doesn't support 1.0 or 1.1; Azure AI services endpoints enforce TLS 1.2, optionally 1.3. None of them has a minimum-TLS setting.
- Python 3.10 and later: `PROTOCOL_TLS_CLIENT`, which `ssl.create_default_context()` uses, has TLS 1.2 as its minimum.
- PSRule for Azure's built-in `Azure.AppService.MinTLS`, `Azure.SQL.MinTLS`, `Azure.Storage.MinTLS` and `Azure.Cosmos.MinTLS` (passes on `Tls12`) check a 1.2 floor and are in the `Azure.GA_2025_12` baseline; no built-in rule checks 1.3.

## Decision

1. **App Service: TLS 1.3.** The API web app `app-docchat-api-<env>-<token>`, the ingestion function app `func-docchat-ingest-<env>-<token>` and the `load` model stub `app-docchat-stub-load-<token>` set `httpsOnly: true`, `siteConfig.minTlsVersion: '1.3'` and `siteConfig.scmMinTlsVersion: '1.3'`. No `minTlsCipherSuite` is set, because a 1.3 floor leaves only the platform's two TLS 1.3 suites, and `endToEndEncryptionEnabled` stays `false`.
2. **Azure SQL: TLS 1.3 with strict encryption.** `sql-docchat-<env>-<token>` sets `minimalTlsVersion: '1.3'`. Every client connects with Microsoft ODBC Driver 18 for SQL Server (`msodbcsql18`, 18.1.2.1 or later, from Microsoft's Debian 12 repository, installed in the API and ingestion images) and `Encrypt=strict` (TDS 8.0), through the one helper `docchat_core.sql.engine.connection_string(service)`: `Driver={ODBC Driver 18 for SQL Server};Server=tcp:<DOCCHAT_SQL_SERVER>,1433;Database=<DOCCHAT_SQL_DATABASE>;Encrypt=strict;ConnectRetryCount=0;APP=docchat-<service>` with `<service>` `api`, `ingestion`, `migrations`, `grants` or `ops`. That covers both apps, the Alembic migrations, `scripts/deploy/sql_principals.py`, `docchat-ops` and the TDS probe of `scripts/deploy/firewall.py open`. No connection to Azure sets `TrustServerCertificate`. Only local-emulator mode (`DOCCHAT_LOCAL_EMULATORS=true`, refused at startup inside Azure with `local_emulators_in_azure`) uses `Encrypt=yes;TrustServerCertificate=yes` against the SQL Server container at `127.0.0.1,14330`.
3. **TLS 1.2 where 1.3 can't be the minimum.** Both storage accounts (`stdocchatdata<env><token>`, `stdocchathost<env><token>`) set `minimumTlsVersion: 'TLS1_2'` and `supportsHttpsTrafficOnly: true`; the Cosmos DB account sets `minimalTlsVersion: 'Tls12'`. Clients that offer 1.3 negotiate 1.3 with both.
4. **Services without a setting.** The environment and test-credential Key Vaults, AI Search, Azure OpenAI, Content Safety, Document Intelligence, the container registry, Log Analytics and Application Insights ingestion and Static Web Apps expose no minimum-TLS property; their platform floor applies (TLS 1.2 for Key Vault, AI Search and the AI services).
5. **Clients.** No first-party code sets a TLS protocol version or cipher list: every HTTPS client (the Azure SDK transports, `httpx`, the `openai` client's transport) uses Python 3.13's default client context, whose minimum is TLS 1.2, and ODBC Driver 18 in strict mode negotiates 1.3 with the server.
6. **Checks.** PSRule (ADR 0106) runs the four built-in rules above and two custom rules in `.ps-rule/docchat.Rule.yaml`: `DocChat.AppService.MinTLS13` (every `Microsoft.Web/sites` has `siteConfig.minTlsVersion` and `siteConfig.scmMinTlsVersion` equal to `'1.3'`) and `DocChat.SQL.MinTLS13` (every `Microsoft.Sql/servers` has `minimalTlsVersion` equal to `'1.3'`). A deployed `dev` test opens a TLS connection to the API's host name with a Python `ssl` client context whose `maximum_version` is `TLSVersion.TLSv1_2` and expects the handshake to fail, then with `minimum_version` `TLSVersion.TLSv1_3` and expects it to succeed.

## Alternatives considered

- **TLS 1.2 on every service.** Rejected: App Service and SQL can refuse anything below 1.3, and every client of theirs speaks 1.3; a 1.3 floor allows only AEAD suites with forward secrecy, encrypts the handshake after ServerHello and has no renegotiation.
- **SQL over TDS 7.4 (`Encrypt=yes`).** Rejected: TDS 7.x caps TLS at 1.2 and exchanges the prelogin before TLS starts; strict mode protects the session from its first byte.
- **`minTlsCipherSuite` on App Service.** Not needed: with a 1.3 floor the platform offers only its two TLS 1.3 suites.
- **End-to-end TLS from App Service's front end to the worker.** Rejected: it exists on Premium plans only, so `dev`'s B1 plan would behave differently from `prod`, and the hop runs inside the platform.
- **A 1.3 floor on Storage or Cosmos DB.** Not possible: neither service accepts 1.3 as a minimum.

## Consequences

- A browser, proxy or tool that can't speak TLS 1.3 can't reach the API. Current Edge, Chrome, Firefox and Safari speak it; an organisation whose TLS-inspecting proxy speaks only 1.2 must exempt the API host from inspection or lower the template's minimum, and the custom rule with it, to `'1.2'`.
- SQL accepts only TDS 8.0 clients. An operator's own SQL tool must connect with strict encryption; `docchat-ops` does. Once set, the SQL minimum can be raised but never returned to `'None'`.
- Storage and Cosmos DB still accept TLS 1.2 from any client; this product's clients negotiate 1.3 with them.
- Obligations: the two custom PSRule rules (84-testing.md); the deployed handshake test; the image smoke step that fails when the ODBC Driver 18 library is missing.

## References

- App Service TLS and minimum versions: https://learn.microsoft.com/en-us/azure/app-service/overview-tls (checked 2026-10-14)
- `Microsoft.Web/sites` properties (API 2025-03-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.web/sites (checked 2026-10-14)
- Azure SQL connectivity settings and minimum TLS: https://learn.microsoft.com/en-us/azure/azure-sql/database/connectivity-settings?view=azuresql (checked 2026-10-14)
- `Microsoft.Sql/servers` (API 2025-01-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.sql/2025-01-01/servers (checked 2026-10-14)
- TDS 8.0 and strict encryption: https://learn.microsoft.com/en-us/sql/relational-databases/security/networking/tds-8 (checked 2026-10-14)
- Storage minimum TLS: https://learn.microsoft.com/en-us/azure/storage/common/transport-layer-security-configure-minimum-version (checked 2026-10-14)
- Cosmos DB minimum TLS: https://learn.microsoft.com/en-us/azure/cosmos-db/self-serve-minimum-tls-enforcement (checked 2026-10-14)
- Key Vault security (TLS 1.2 and 1.3): https://learn.microsoft.com/en-us/azure/key-vault/general/secure-key-vault (checked 2026-10-21)
- AI Search built-in protections (TLS): https://learn.microsoft.com/en-us/azure/search/search-security-built-in (checked 2026-10-21)
- Azure AI services security features (TLS): https://learn.microsoft.com/en-us/azure/ai-services/security-features (checked 2026-10-21)
- Python `ssl` module defaults: https://docs.python.org/3.13/library/ssl.html (checked 2026-10-21)
- PSRule rule `Azure.Cosmos.MinTLS`: https://azure.github.io/PSRule.Rules.Azure/en/rules/Azure.Cosmos.MinTLS/ (checked 2026-10-21)
