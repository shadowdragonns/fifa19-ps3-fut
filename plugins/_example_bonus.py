"""Disabled example: copy to win_bonus.py to enable a flat win bonus."""


def register(api):
    def current_bonus():
        bonus = api.settings.get('bonus_coins', 250)
        if type(bonus) is not int or not 0 <= bonus <= 1000000:
            raise ValueError('bonus_coins must be an integer from 0 to 1000000')
        return bonus
    current_bonus()

    @api.filter('match.reward')
    def reward(event, coins):
        return coins+current_bonus() if event['result'] == 'WIN' else coins

    @api.on('match.completed')
    def completed(event):
        api.log(f"Match {event['match_id']}: {event['player']['club_name']} "
                f"{event['result']}, awarded {event['coins']} coins")
