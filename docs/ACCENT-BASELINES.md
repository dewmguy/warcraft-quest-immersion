# Accent baselines

## Purpose

These are production defaults for reusable race/gender voices, not claims that every member of a Warcraft race speaks identically. NPC-specific voices may override them when recorded dialogue, birthplace, social class, or story context provides better evidence.

Gender presentation changes pitch and resonance only. It does not change the accent target, so the 49 reviewed race and model-family contracts cover all 98 male/female baseline profiles.

## Research method

1. Treat the English-language in-game player and generic NPC vocal sets as the primary performance reference.
2. Use the race's established game presentation only to resolve broad intent; do not infer a dialect from architecture or real-world cultural coding alone.
3. Inherit the parent race's accent for display-derived categories such as Fel Orc, Broken, Forest Troll, Taunka, Northrend Skeleton, and Ice Troll.
4. Use neutral North American English when the display category has no stable regional performance. Put supernatural qualities in timbre and cadence, not a fabricated ethnic accent.
5. State exclusions explicitly. ElevenLabs recommends naming the native language and regional variant first because vague words such as “English,” “polished,” or “fantasy” can cause dialect drift.

The machine-readable source of truth is [`voice_profiles/race-archetypes.json`](../voice_profiles/race-archetypes.json). `accent_target`, `accent_avoid`, and `accent_basis` remain separate so research notes are not confused with delivery direction.

## Reviewed matrix

| Race/display category | Baseline accent | Basis |
|---|---|---|
| Narrator | Neutral General American | Conservative system fallback |
| Human | Modern General American | Vanilla player and NPC vocal sets |
| Orc | Modern General American | Vanilla player and NPC vocal sets |
| Dwarf | Light broad Scottish | Vanilla player and NPC vocal sets |
| Night Elf | American base with light classic Mid-Atlantic stage polish | Vanilla player and NPC vocal sets |
| Undead / Scourge | Neutral General American with light old-stage crispness | Vanilla player and NPC vocal sets |
| Tauren | Neutral General American | Vocal-set review; cadence carries cultural weight |
| Gnome | Modern General American | Vanilla player and NPC vocal sets |
| Troll | Light Jamaican or Anglophone-Caribbean influence | Vanilla player and NPC vocal sets |
| Goblin | Light New York City metropolitan | Goblin NPC and Cataclysm player vocal sets |
| Blood Elf | Fully rhotic modern General American; affluent, educated, old-money register | TBC player/NPC sets and the supplied female reference clips |
| Draenei | Light Slavic or Eastern European | TBC player and NPC vocal sets |
| Fel Orc | Orc baseline | Parent inheritance |
| Naga | Neutral General American | Conservative fallback across varied NPC performances |
| Broken | Draenei baseline | Parent inheritance |
| Skeleton | Neutral General American | Former mortal identity is unknown at display level |
| Vrykul | Light Scandinavian or Nordic | Representative Wrath performances and Nordic presentation |
| Tuskarr | Regionally neutral North American | Conservative fallback; avoids ethnic mimicry |
| Forest Troll | Troll baseline | Parent inheritance |
| Taunka | Tauren baseline | Parent inheritance |
| Northrend Skeleton | Skeleton baseline | Parent inheritance |
| Ice Troll | Troll baseline | Parent inheritance |
| Worgen | Light modern RP or class-neutral southern British | Cataclysm Gilnean and Worgen vocal sets |

## Blood Elf correction

“Polished English” was too ambiguous for Voice Design and allowed a strong British interpretation. The corrected contract begins with modern United States English, requires fully rhotic R sounds, places refinement in class register and delivery, and explicitly excludes Received Pronunciation, English regional accents, Mid-Atlantic/Transatlantic vowels, Scottish, and Irish drift.

The class cue is deliberately separate from the dialect cue: confident privilege should affect attitude and phrase shaping without changing the voice into British aristocracy.

## Evidence and prompting references

- [Blizzard playable-race registry](https://worldofwarcraft.blizzard.com/en-gb/game/races) — canonical race scope and parent relationships.
- [Wowhead Classic sound index](https://www.wowhead.com/classic/sounds) — in-game vocal and generic NPC sound kits, including Human, Orc, Tauren, Night Elf, Troll, and other legacy sets.
- [Human male official NPC greetings](https://www.wowhead.com/classic/sound=5971/humanmaleofficialnpcgreetings), [Dwarf male vocal set](https://www.wowhead.com/classic/sound=6107/dwarf-male-vocal-12-hello), and [Troll male NPC greetings](https://www.wowhead.com/classic/sound=5943/trollmaledarknpcgreetings) — representative indexed game assets.
- [Blood Elf female vocal set](https://www.wowhead.com/tbc/sound=9642/bloodelffemalevocalflirt) and [Draenei male NPC vocal set](https://www.wowhead.com/sound=9767/draeneimalenoblepissed) — representative TBC-era game assets.
- [Worgen race page and recorded voice references](https://www.wowhead.com/race=22/worgen) — British/Gilnean performance evidence.
- [Community accent comparison](https://eu.forums.blizzard.com/en/wow/t/the-accents-of-wow/446841) — a secondary perception check used only where it agrees with the recorded sets.
- [ElevenLabs Voice Design prompting guide](https://elevenlabs.io/docs/eleven-creative/voices/voice-design/) — explicit language/dialect-first prompt structure, granular voice attributes, and dialect-drift cautions.

This is a reviewed baseline, not a frozen conclusion. A new source-audio finding should update the race contract, regenerate the profile matrix, and increment the baseline context revision so deployed records receive a reversible new metadata version.
