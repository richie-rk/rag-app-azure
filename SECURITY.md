# Security policy

## Reporting a vulnerability

Report a suspected vulnerability only through GitHub private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. That form is the only reporting channel.

Never open a public issue, pull request or discussion for a vulnerability, and never put exploit details, credentials or personal data in one. A report sent through any other channel is answered with a request to resubmit it privately.

Include what you can of: the affected component and version (the commit SHA), the steps that reproduce the problem, what an attacker gains, and any suggested fix.

## What to expect

- A first response within 5 business days of the report.
- Updates in the private advisory while the report is triaged and fixed.
- Credit in the published advisory, if you want it, once a fix is released.

## Supported version

Only the release deployed from `main` is supported. Fixes are made on `main` and released through the normal deployment pipeline; earlier releases receive no separate fixes.
