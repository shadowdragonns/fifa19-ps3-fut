"""Copy to presence.py to enable join/leave logging and club queries."""


def register(api):
    @api.on('player.online')
    def joined(event):
        person = event['player']; persona = person['persona_id']
        club = api.get_club(persona)
        cards = api.get_cards(persona, pile='club', min_rating=85, count=100)
        api.log(f"{person['name']} joined: {club['name']}, {club['coins']} coins, "
                f"{len(cards)} cards rated 85+ (first 100), {len(api.get_players(online=True))} online (first 100)")

    @api.on('player.fut_entered')
    def entered(event):
        squad = api.get_squad(event['player']['persona_id'])
        api.log(f"Entered FUT with squad {squad['name']}")

    @api.on('player.offline')
    def left(event):
        api.log(f"{event['player']['name']} left ({event['reason']})")
