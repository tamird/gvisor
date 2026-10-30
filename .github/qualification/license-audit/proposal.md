# Preserve LicenseRef audits for pinned artifacts

The CodeQL CLI bundle ships its own terms and component notices. Fetching the license of the github/codeql-action repository that hosts it produces MIT. The fork’s two libcrypt1 packages likewise carry per-file notices whose terms are recorded together. [CodeQL terms](https://github.com/github/codeql-cli-binaries/blob/778dd2254/LICENSE.md) · [Hosting license](https://github.com/github/codeql-action/blob/416ff0dea/LICENSE)

Accept a local SPDX LicenseRef for an artifact audit and retain its complete catalog entry while the URL and SHA256 match. A changed pin requires a new audit. An explicit license_source continues to fetch and classify the immutable notice. An entry that requests both modes fails, as does a duplicate entry that could hide the audit. License policy still checks whether the recorded terms are permitted.

Compared with the previous proposal, this removes the manually_audited field. Standard terms already have identifiers. For example, Puma’s BSD and MIT notices are available as immutable upstream files, so support for multiple pinned notices belongs with that consumer. [Puma BSD notice](https://github.com/puma/puma/blob/5a58290f0/LICENSE) · [Puma MIT notice](https://github.com/puma/puma/blob/5a58290f0/lib/puma/sd_notify.rb)
