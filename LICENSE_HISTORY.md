# License history

## 2026-04-28: MIT → AGPL-3.0-or-later

Ghost-hunter was originally released under the MIT License. On
**2026-04-28**, the project was relicensed to the **GNU Affero
General Public License v3.0 or later (AGPL-3.0-or-later)**.

### Why

The MIT license is permissive but provides no protection against the
"open-source SaaS arbitrage" pattern: a third party clones the
repository, hosts it as a paid service, and contributes nothing back.

This pattern has weakened the open-source ecosystem repeatedly:
MongoDB, Elastic, Redis, CockroachDB, Sentry, and many others have
shifted away from permissive licenses for the same reason.

AGPL-3.0 is a free, OSI-approved open-source license that protects
against this pattern: anyone who hosts a modified version of
Ghost-hunter as a service must release their modifications under
AGPL-3.0. Internal use, individual use, and modifications that are
not hosted publicly are unaffected.

### What this means

| You are... | Effect |
|---|---|
| An individual developer trying Ghost-hunter on your own bill | No change — install and use freely |
| A team running Ghost-hunter internally on your own cloud accounts | No change — internal use is fine |
| A company building features on top of Ghost-hunter for internal tooling | No change |
| A SaaS provider hosting Ghost-hunter (modified or unmodified) as a paid service | Must release modifications under AGPL-3.0 |
| A consultant using Ghost-hunter to deliver client work | No change — this is internal use |

### Backwards compatibility

Versions **prior to v1.0.6** remain available under their original MIT
license. The MIT license text is preserved at `LICENSE.MIT.original`.

If you depend on the MIT-licensed version of Ghost-hunter, you may
continue to use any release tagged at or before v1.0.6 under MIT
terms.

**Starting with v1.0.7**, Ghost-hunter is licensed under
AGPL-3.0-or-later.

### References

- [GNU AGPL-3.0 official text](https://www.gnu.org/licenses/agpl-3.0.txt)
- [OSI on AGPL-3.0](https://opensource.org/license/agpl-v3)
- [Why companies relicense to AGPL: Sentry's reasoning](https://blog.sentry.io/relicensing-sentry/)
