"""Configured performance coins using the retail PS3 matchCoinPartials schema."""
# Retail 0x16b1cc8 enum order; value is coins/unit, quantity is capped units,
# and total is their signed product. Keep the native names for UI labels.
ROWS = (
    ('goals','GOALS',100,5),
    ('shots_on_target','SHOTS_ON_TARGET',5,10),
    ('successful_tackles','SUCCESSFUL_TACKLES',1,25),
    ('corners','CORNERS',5,10),
    ('clean_sheet','CLEANSHEETS',150,1),
    ('passing_percentage','PASSING_PERCENTAGE',1,80),
    ('possession_percentage','POSSESSION_PERCENTAGE',1,60),
    ('man_of_the_match','MAN_OF_THE_MATCH',15,1),
    ('goals_against','GOALS_AGAINST',-20,3),
    ('fouls','FOULS',-5,5),
    ('yellow_cards','YELLOW_CARDS',-10,5),
    ('red_cards','RED_CARDS',-40,5),
    ('offsides','OFFSIDES',-1,10))
FIELDS = ('goals','shotsOnTarget','successfulTackles','corners','cleansheets',
          'passingPercentage','possessionPercentage','manOfTheMatch',
          'goals','fouls','yellowCards','redCards','offsides')


def defaults():
    return {'enabled':True, **{name:{'coins':coins,'cap':cap} for name,_,coins,cap in ROWS}}


def validate(rules):
    if (not isinstance(rules,dict) or set(rules)!=set(defaults())
            or type(rules['enabled']) is not bool):
        raise ValueError('Invalid match_bonuses settings')
    maximum=0
    for name,_,default,_ in ROWS:
        rule=rules[name]
        limit=1 if name in ('clean_sheet','man_of_the_match') else 100 if name.endswith('_percentage') else 1000
        if (not isinstance(rule,dict) or set(rule)!={'coins','cap'}
                or type(rule['coins']) is not int or type(rule['cap']) is not int
                or not 0<=rule['cap']<=limit
                or not (-10000<=rule['coins']<=0 if default<0 else 0<=rule['coins']<=10000)):
            raise ValueError('Invalid match_bonuses.'+name)
        maximum+=max(0,rule['coins'])*rule['cap']
    if maximum>1000000:raise ValueError('Match performance bonuses must not exceed 1000000 coins')
    return maximum if rules['enabled'] else 0


def partials(report,rules):
    """The caller validates the report; never count opponent stats as ours."""
    if not rules['enabled'] or report['endReason'] not in ('WIN','DRAW','LOSS'):return []
    ours,theirs=report['myMatchStats'],report['opponentMatchStats']
    rows=[]
    for (name,kind,_,_),field in zip(ROWS,FIELDS):
        if name=='clean_sheet':quantity=int(theirs['goals']==0)
        elif name=='goals_against':quantity=theirs['goals']
        else:quantity=ours[field]
        rule=rules[name];quantity=min(quantity,rule['cap'])
        rows.append({'type':kind,'value':rule['coins'],'quantity':quantity,
                     'total':rule['coins']*quantity})
    return rows


def bonus(report,rules):
    # Discipline/conceded-goal deductions cannot take the normal result pay.
    return max(0,sum(row['total'] for row in partials(report,rules)))
