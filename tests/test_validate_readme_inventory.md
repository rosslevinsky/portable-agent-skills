# Fixture: README inventory drifted from skills/

Synthetic README whose skill inventory disagrees with the harness's skills list
(`alpha-skill`, `beta-skill`, `gamma-skill`): the table lists a retired skill,
omits a real one, and the hardcoded prose count is stale. The README-inventory
rule must reject all three drifts.

One install command drops 4 skills into the right places for both runtimes.

## Skill Inventory

| Skill | Description | Classification |
|---|---|---|
| `alpha-skill` | A real skill | Full |
| `beta-skill` | Another real skill | Full |
| `retired-skill` | No longer exists under skills/ | Full |

## Installation

Nothing below the inventory section matters to the rule.
