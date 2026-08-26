# Fixture: README inventory in sync with skills/

Synthetic README whose skill inventory agrees with the harness's skills list
(`alpha-skill`, `beta-skill`, `gamma-skill`) and whose prose count matches. The
README-inventory rule must pass it.

One install command drops 3 skills into the right places for both runtimes.

## Skill Inventory

| Skill | Description | Classification |
|---|---|---|
| `alpha-skill` | A real skill | Full |
| `beta-skill` | Another real skill | Full |
| `gamma-skill` | A third real skill | Degraded |

## Installation

Nothing below the inventory section matters to the rule.
