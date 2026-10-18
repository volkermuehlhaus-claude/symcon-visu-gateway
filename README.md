# symcon-visu-gateway

Abgesichertes MCP-Gateway zu IP-Symcon, um per Claude Code eine Kachelvisualisierung aufzubauen.
Fork von [Schimmilab/ipsymcon-mcp-server](https://github.com/Schimmilab/ipsymcon-mcp-server) (MIT), stark reduziert:
`client.py` stammt vom Upstream, alles andere ist neu. Lizenz und Urheberhinweis siehe `LICENSE`.

## Unterschiede zum Upstream

| Upstream | Dieses Gateway |
|---|---|
| generischer `ips_call` | entfällt; nur gelistete Funktionen (`guard.py`) |
| Skripte lesen/anlegen/ausführen, Export mit `IPS_GetConfiguration` | entfällt |
| Werte setzen, Aktionen auslösen | entfällt |
| Schreiben: ein Ein/Aus-Schalter | Schreiben nur unterhalb der Zielkategorie, Quelle **und** Ziel geprüft, Audit-Log vor jeder Änderung |
| keine Filter | Secret-Filter auf alle Antworten (`secret_filter.py`) |
| Zugangsdaten in `.env`, stdio im Konto des Clients | Konfigurationsdatei im Konto des Gateways, HTTPS + Token (nur SHA-256-Hash gespeichert) |

## Betrieb

```
python -m visu_gateway --transport stdio --config config.yaml   # nur lokales Testsystem
python -m visu_gateway --transport https --config config.yaml   # auf dem Symcon-Rechner
python -m visu_gateway.token                                    # Token + Hash erzeugen
```

Tests: `pip install -r requirements-dev.txt && pytest`.
