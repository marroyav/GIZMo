# Security policy

Do not commit credentials, private keys, certificates, production endpoints,
private-network addresses, device identities, live databases, device images,
calibration/state bundles, or controlled recovery artifacts.

Report a suspected exposure through the project's controlled security channel
or GitHub private vulnerability reporting. Do not place a secret or sensitive
topology in a public issue.

If authentication material is exposed, revoke or rotate it immediately.
Deleting a file or rewriting Git history does not restore the secrecy of a
credential that another party may already have copied.

`tests/test_publication_safety.py` enforces the repository's public boundary.
A full device build must obtain excluded inputs from the separately controlled
asset workflow described in `README.md`.
