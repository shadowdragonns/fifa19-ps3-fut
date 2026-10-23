# Single-player tournaments

Ultimate Team → Single Player → Tournaments uses three local knockout cups:

| Cup | Difficulty | Opponents | Champion bonus |
|---|---|---|---|
| Local Starter Cup | Amateur | Lower rated clubs | 1,000 coins |
| Local Champions Cup | Professional | Strong clubs | 2,500 coins |
| Local World Cup | World Class | International sides, then strong clubs | 5,000 coins |

Each draw has your club and fifteen CPU teams, with four rounds. The usual
configured match reward is paid separately. Four wins earn the champion bonus
once; a loss or abandoned match ends the attempt. A new attempt starts a fresh
bracket. Completed cups and current progress are saved per RPCN club.

The retail client generates the bracket and uploads its serialized data. The
server stores that data for reopening the menu, but uses committed owned-match
results to advance wins and award coins. Uploads cannot award coins, and retrying
a result or bracket save cannot pay twice. Match conditions, goals, appearances,
contracts and discipline use the same result handling as CPU seasons. No new
players are granted by entering a cup.

The native tournament list, opponent, load, update and quit routes are handled.
Tournament definitions/opponent arrays have been checked against the retail
PS3 parsers. Menu navigation, bracket scheduling, extra time/penalties and the
full four-match flow still require in-game verification. Online tournaments
are outside this implementation.

Retail's empty auxiliary progress field (`progressDataVersion: 2`,
`progressData: "AAAAAA=="`) is accepted as an empty sized envelope. Repeated
initial bracket saves are allowed without advancing wins or awarding coins.

Final-match creation accepts retail's `tournamentTrophyRound: "true"` string.
Boolean values and legacy 0/1 flags are normalized for reservation retries.
The flag is informational: only four committed wins authorize the cup prize.
