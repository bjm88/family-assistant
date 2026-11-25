"""Family games played together from phones.

* :mod:`.catalog` — the list of games and the turkeys players pick.
* :mod:`.turkey_tictactoe` — rules of the first game (pure functions).
* :mod:`.service` — starting, joining and playing a game against the DB.
* :mod:`.invites` — texting / emailing a player their join link.
* :mod:`.hub` — the live WebSocket connections of each game.

The HTTP and WebSocket routes are in :mod:`api.routers.games`.
"""
