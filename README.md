# Champ Picker

A personal League of Legends draft helper. It does u.gg-style lookups (counters, tier list) from match data you collect yourself, and during champ select it ranks the best picks **from your own champion pool**, weighing:

- how each pick does against the enemy champions locked so far (your lane opponent counts most),
- how it does with the allies already picked or hovered,
- gaps in your team's comp (all physical damage, no frontline, no crowd control),
- your own recent games and mastery on that champion,
- how safe it is as a blind pick when the enemy laner isn't known yet.

It shows the top 3 with an estimated win chance and the biggest reasons. It never picks, bans or locks for you, and it never reads other players' names. That keeps it inside Riot's rules for third-party tools.

Design notes: [League best-pick helper: design](https://claude.ai/artifact/C3GVs4pnf7hnCj31Sy4KfB)

## What you need

- Windows PC with League installed
- Python 3.10 or newer (python.org; tick "Add python.exe to PATH"). No other packages needed.
- A Riot API key from the Riot Developer Portal (sign in with your Riot account). The free development key expires every 24 hours; registering a personal product there gets a key that lasts.

## Set up

Clone this repo, then from its folder:

```powershell
git clone https://github.com/Jayqub99/champpicker
cd champpicker
copy config.example.json config.json
notepad config.json            # set riot_id, e.g. "Name#NA1"
$env:RIOT_API_KEY = "RGAPI-..." # or put "api_key" in config.json (it's git-ignored)
```

`config.json` and the `data` folder never get committed.

## Fill the database

```powershell
python -m pickhelper sync-me                   # your mastery + last ~100 ranked games
python -m pickhelper seed                      # ladder players at the tiers in config.json
python -m pickhelper crawl --max-requests 3000 # download matches (about an hour at personal-key limits)
```

Run `crawl` again whenever you like. It skips matches it already has. A personal key allows 100 requests every 2 minutes, so roughly 50 matches a minute. A few thousand matches are enough for common ADC matchups; rare ones show "little data".

## Use it

```powershell
python -m pickhelper serve
```

Open http://127.0.0.1:8777. Leave the **Live pick** tab open, start a game, and picks update as champ select changes. The **Pick builder** tab lets you try a draft by hand, **Counters** shows a champion's hardest and easiest lane matchups, and **Tier list** ranks a role.

Terminal version:

```powershell
python -m pickhelper recommend --enemies "Draven,Nautilus" --allies "Thresh"
```

## Settings (`config.json`)

| Key | Meaning |
| --- | --- |
| `riot_id` | Your Riot ID, used by `sync-me` |
| `role` | Your main role: `TOP`, `JUNGLE`, `MIDDLE`, `BOTTOM` or `UTILITY` |
| `pool` | Optional fixed list of champion names. Empty means "champions I've played 2+ times in this role recently" |
| `crawl_tiers` | Ladder tiers to sample, e.g. `EMERALD`, `DIAMOND`, `MASTER` |
| `patches_to_use` | How many recent patches feed the stats |

## Tests

```powershell
python -m unittest discover -s tests
```

## Notes

- Champ select is read from the League client's local API. Riot doesn't officially support it, so a client patch can break it; the rest of the app keeps working.
- Riot asks that apps using the local client API be registered in the Developer Portal with the endpoints used. This app reads only `/lol-champ-select/v1/session`.
- Pick Helper isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
