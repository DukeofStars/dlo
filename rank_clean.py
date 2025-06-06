from openskill.models import PlackettLuce, PlackettLuceRating
from typing import TypedDict, Any, Dict, List, Tuple, Optional
from datetime import datetime
import shutil
import os
import xml.etree.ElementTree as ET
import glob
import itertools
import random
import string
from pathlib import Path
import json
import html
import plotly.express as px
import plotly.io as pio
import plotly.graph_objects as go
from plotly.subplots import make_subplots

class PlayerInfo(TypedDict):
    player_id: str
    steam_name: str
    faction: str
    fleet_file_path: Path

class PlayerData(TypedDict):
    steam_name: str
    rating_data: PlackettLuceRating
    player: bool
    games_played: int
    wins: int
    ans_games: int
    ans_wins: int
    osp_games: int
    osp_wins: int
    history: List[Tuple[datetime, float]]
    teammates: Dict[str, Dict[str, int]]

class MatchData(TypedDict):
    valid: bool
    time: datetime
    teams: Dict[str, PlayerInfo]
    winning_team: str
    avg_dlo: float
    match_quality: float

HistogramType = Dict[str, Dict[int, float]]

def collect_fleet_files():
    """store fleet files from people on the whitelist"""
    fleet_file_dir = Path("/srv/steam/.steam/steam/steamapps/common/NEBULOUS Dedicated Server/Saves/Fleets/")
    whitelist_file_path = (Path(__file__).parent).joinpath('fleet_dlo_whitelist.txt')
    whitelist = []
    with open(whitelist_file_path, 'r') as f:
        for line in f:
            steam_id = line.rstrip('\r\n')
            if steam_id.isdigit():
                whitelist.append(steam_id)

    for fleet_file_path in fleet_file_dir.iterdir():
        if any(steam_id in fleet_file_path.name for steam_id in whitelist):
            print("INFO: saving fleet:", fleet_file_path.name)
            shutil.copy(fleet_file_path, (Path(__file__).parent).joinpath('docs/fleets'))
        else:
            print("WARN: fleet not in whitelist", fleet_file_path.name)
            continue


def parse_battle_report(file_path: Path) -> MatchData:
    ANS_HULLKEYS = [
        'Stock/Sprinter Corvette',
        'Stock/Raines Frigate',
        'Stock/Keystone Destroyer',
        'Stock/Vauxhall Light Cruiser',
        'Stock/Axford Heavy Cruiser',
        'Stock/Solomon Battleship',
        'Stock/Levy Escort Carrier']

    OSP_HULLKEYS = [
        'Stock/Shuttle',
        'Stock/Tugboat',
        'Stock/Journeyman',
        'Stock/Bulk Feeder',
        'Stock/Ore Carrier',
        'Stock/Ocello Cruiser',
        'Stock/Bulk Hauler',
        'Stock/Container Hauler',
        'Stock/Container Hauler Refit']

    game_time = parse_skirmish_report_datetime(Path(os.path.split(file_path)[1]))

    with open(file_path) as fp:
        xml_string = fp.read()
        # handle edge cases where xml encoding format doesn't match or extra data is leftover after BR
        last_gt_index = xml_string.rfind('>')
        if last_gt_index != -1:
            xml_string = xml_string[:last_gt_index + 1]
        tree = ET.ElementTree(ET.fromstring(xml_string))
    root = tree.getroot()

    # validate basic params about game. if start timestamp is 0 game didn't start. If game duration is super long or super short something probably went wrong
    if int(root.find('GameStartTimestamp').text) == 0 or int(root.find('GameDuration').text) > 7000 or int(root.find('GameDuration').text) < 200 or not bool(root.find('GameFinished').text):
        return {
            "valid": False,
            "time": game_time,
            "teams": {},
            "winning_team": 'None',
            "avg_dlo": 0.0,
            "match_quality": 0.0}

    teams_data: Dict[str, List[PlayerInfo]] = {}
    
    for team_element in root.findall('./Teams/*'):
        team_id_element = team_element.find('TeamID')
        team_id = team_id_element.text if team_id_element is not None else ''
        players: List[PlayerInfo] = []

        team_faction = ''

        for player_element in team_element.findall('./Players/*'):
            player_name_element = player_element.find('PlayerName')
            account_id_element = player_element.find('AccountId/Value')

            player_name = html.escape(player_name_element.text) if player_name_element is not None else ''
            player_id = html.escape(account_id_element.text) if account_id_element is not None else ''

            # find out if the player was ANS or OSP. This then updates the team_faction which gets applied after all players are parsed
            # This ensures even if one player DCs and loses all ships, we still report their original faction correctly
            player_hullkeys = player_element.findall('./Ships/ShipBattleReport/HullKey')
            is_player_ANS = all(k.text in ANS_HULLKEYS for k in player_hullkeys) and len(player_hullkeys)
            is_player_OSP = all(k.text in OSP_HULLKEYS for k in player_hullkeys) and len(player_hullkeys)

            if is_player_ANS and team_faction != 'OSP':
                team_faction = 'ANS'
            if is_player_OSP and team_faction != 'ANS':
                team_faction = 'OSP'
            
            players.append({
                'player_id': player_id,
                'steam_name': player_name,
            })

        # assign team faction at the end to catch any players who had zero ships in the battle report
        for p in players:
            print(p)
            p['faction'] = team_faction
       
        for p in players:
            assert p['faction'] in ['ANS', 'OSP']

        teams_data[team_id] = players

    winner_element = root.find('WinningTeam')
    winner = winner_element.text if winner_element is not None else ''
    return {
            "valid": True,
            "time": game_time,
            "teams": teams_data,
            "winning_team": winner,
            "avg_dlo": 0.0,
            "match_quality": 0.0
            }

def process_match_result(
    match_data: MatchData,
    match_history: List[MatchData],
    database: Dict[str, PlayerData],
    model: PlackettLuce,
) -> Dict[str, List[PlackettLuceRating]]:
    """updates database with all non DLO info, and returns lists of rating objects for later DLO scoring"""
    updated_teams: Dict[str, List[PlackettLuceRating]] = {}

    winner = match_data["winning_team"]

    if not match_data['valid'] or winner not in match_data['teams']:
        print(f"ERROR: invalid report found at {match_data['time']}")
        return

    # create teams of rating objects for ranking, and store match data to DB
    for team_id, players in match_data['teams'].items():
        team_players: List[PlackettLuceRating] = []
        
        for player in players:
            player_id = player['player_id']
            steam_name = player['steam_name']

            if player_id not in database:
                print(f"Found new player: {steam_name} {player_id}")
                database[player_id] = {
                    "steam_name": steam_name,
                    "rating_data": model.rating(name=player_id),
                    "player": True,
                    "games_played": 0,
                    "wins": 0,
                    "ans_games": 0,
                    "ans_wins": 0,
                    "osp_games": 0, 
                    "osp_wins": 0, 
                    "history": [],
                    "teammates": {}
                }
            team_players.append(database[player_id]['rating_data'])

            # update total games played and wins
            database[player_id]['games_played'] += 1
            if team_id == winner:
                database[player_id]['wins'] += 1

            # update faction games played and wins
            assert(player['faction'] in ['ANS', 'OSP'])
            if player['faction'] == 'ANS':
                database[player_id]['ans_games'] += 1
                if team_id == winner:
                    database[player_id]['ans_wins'] += 1
            if player['faction'] == 'OSP':
                database[player_id]['osp_games'] += 1
                if team_id == winner:
                    database[player_id]['osp_wins'] += 1

            for teammate in players:
                teammate_id = teammate['player_id']
                if player_id == teammate_id:
                    continue
                db = database[player_id]['teammates']
                if teammate_id not in iter(db):
                    db[teammate_id] = {"games": 0, "wins": 0}
                db[teammate_id]["games"] += 1
                if team_id == winner:
                    db[teammate_id]["wins"] += 1
        updated_teams[team_id] = team_players 

    winner_team = updated_teams.pop(winner)
    other_team = updated_teams[next(iter(updated_teams))]

    for player_rating in winner_team + other_team:
        player_id = player_rating.name
        database[player_id]['history'].append((
            match_data["time"],
            database[player_id]['rating_data'].ordinal()
        ))

    # add synthetic players to balance team sizes
    largest_team = max(len(winner_team), len(other_team))

    if len(winner_team) < largest_team:
        average_mu = sum([p.mu for p in winner_team]) / len(winner_team)
        average_sigma = sum([p.sigma for p in winner_team]) / len(winner_team)

        for i in range(0, largest_team - len(winner_team)):
            winner_team.append(model.rating(
                name = "TEST_PLAYER",
                mu=average_mu,
                sigma=average_sigma
            ))

    if len(other_team) < largest_team:
        average_mu = sum([p.mu for p in other_team]) / len(other_team)
        average_sigma = sum([p.sigma for p in other_team]) / len(other_team)

        for i in range(0, largest_team - len(other_team)):
            other_team.append(model.rating(
                name = "TEST_PLAYER",
                mu=average_mu,
                sigma=average_sigma
            ))

    # grab dlo metrics for match before scoring
    average_mu = sum([p.mu for p in itertools.chain(winner_team, other_team)]) / (len(winner_team) + len(other_team))
    average_sigma = sum([p.sigma for p in itertools.chain(winner_team, other_team)]) / (len(winner_team) + len(other_team))
    average_dlo = average_mu - 3.0 * average_sigma
    match_quality = model.predict_draw([winner_team, other_team])

    match_data['avg_dlo'] = average_dlo
    match_data['match_quality'] = match_quality

    rated_teams = model.rate([winner_team, other_team])

    # write ranking update back to database
    for team in rated_teams:
        for player in team:
            if player.name == 'TEST_PLAYER':
                continue
            player_id = player.name
            database[player_id]['rating_data'] = player
            database[player_id]['history'][-1] = (
                match_data['time'],
                player.ordinal()
            )

    # store match to match history
    match_history.append(match_data)

def update_histogram(
    histogram: HistogramType,
    database: Dict[str, PlayerData],
    game_index: int
) -> None:
    for player_id, data in database.items():
        if data['player']:
            # Changed to use player_id as key
            histogram[player_id][game_index] = data['rating_data'].ordinal()

def render_leaderboard(
    database: Dict[str, PlayerData],
    output_html: bool = True
) -> None:
    """Display sorted leaderboard and generate static HTML"""
    leaderboard = sorted(database.values(), 
                        key=lambda d: d["rating_data"].ordinal(), 
                        reverse=True)
    
    print("\nLEADERBOARD:")
    for p in leaderboard:
        print(f"{p['steam_name']:20} DLO = {p['rating_data'].ordinal():6.2f} "
              f"Matches: {p['games_played']}")

    if output_html:
        player_dir = Path('docs/player')
        player_dir.mkdir(exist_ok=True, parents=True)
        
        # Generate individual player pages
        for player_id, data in database.items():
            render_player_page(player_id, data, database)

        # Main leaderboard HTML
        html_content = f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DLO Leaderboard</title>
    <style>
        body {{
            font-family: monospace;
            margin: 2rem;
            background-color: #1a1a1a;
            color: #e0e0e0;
        }}
        .header {{
            border-bottom: 2px solid #3a3a3a;
            margin-bottom: 1rem;
            padding-bottom: 1rem;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background-color: #2d2d2d;
            border-radius: 8px;
            overflow: hidden;
        }}
        th, td {{
            padding: 0.75rem 1rem;
            border: 1px solid #3a3a3a;
            text-align: left;
        }}
        th {{
            background-color: #333333;
            color: #00cc99;
            font-weight: 600;
        }}
        tr:nth-child(even) {{
            background-color: #262626;
        }}
        tr:hover {{
            background-color: #363636;
            transition: background-color 0.2s ease;
        }}
        a {{
            color: #00ccff; 
            text-decoration: none;
            font-weight: 500;
        }}
        a:hover {{
            color: #00ffff;
            text-decoration: underline;
        }}
        h1 {{
            color: #ffffff;
            margin: 0.5rem 0;
        }}
    </style>
</head>
<body>
    <div class="header">
        <img src="dlo.webp" alt="Logo" width="150">
        <h1>Player Leaderboard</h1>
        <a href="https://openskill.me/en/stable/manual.html">Ranking System Info</a> 
        | <a href="index.html">Player Leaderboard</a>
        | <a href="rank_distribution.html">DLO Rank Distributions</a>
        | <a href="match_history.html">Match History</a>
    </div>
    
    <table>
        <thead>
            <tr>
                <th>Rank</th>
                <th>Player</th>
                <th>DLO</th>
                <th>Matches Played</th>
                <th>μ ± σ</th>
            </tr>
        </thead>
        <tbody>
            {"".join(
                f'<tr><td>{i+1}</td>'
                f'<td><a href="player/{p["rating_data"].name}.html">{p["steam_name"]}</a></td>'
                f'<td>{p["rating_data"].ordinal():0.2f}</td>'
                f'<td>{p["games_played"]}</td>'
                f'<td>{p["rating_data"].mu:0.2F} ± {p["rating_data"].sigma:0.2f}</td></tr>'
                for i, p in enumerate(leaderboard)
            )}
        </tbody>
    </table>
</body>
</html>
        '''

        output_path = Path('docs/index.html')
        output_path.write_text(html_content)
        print(f"\nGenerated static site at: {output_path.absolute()}")

def get_best_friends(player_data: PlayerData, database: Dict[str, PlayerData]) -> List[Dict[str, Any]]:
    teammates = []
    for teammate_id, stats in player_data['teammates'].items():
        # Skip teammates with zero wins
        if stats['wins'] == 0:
            continue
            
        if teammate_id in database:
            win_rate = stats['wins'] / stats['games']
            teammates.append({
                'id': teammate_id,
                'name': database[teammate_id]['steam_name'],
                'games': stats['games'],
                'wins': stats['wins'],
                'win_rate': win_rate
            })
    
    # Sort by win rate (descending), then games played (descending)
    sorted_teammates = sorted(teammates, 
                            key=lambda x: (-x['wins'], -x['win_rate']))
    
    return sorted_teammates[:3]  # Return top 3 (or fewer if less available)

def render_player_page(
    player_id: str,
    player_data: PlayerData,
    database: Dict[str, PlayerData]
) -> None:
    """Generate individual player page with stats and history graph"""

    plot_dir = Path('docs/player/images')
    plot_dir.mkdir(exist_ok=True, parents=True)
    
    #plot_path = img_dir / f'{player_id}_history.webp'
    plot_path = plot_dir / f'{player_id}_history.html'
    generate_dlo_plot(player_data, plot_path)
    
    total_games = player_data['games_played']
    losses = total_games - player_data['wins']
    win_rate = player_data['wins'] / total_games if total_games > 0 else 0

    ans_losses = player_data['ans_games'] - player_data['ans_wins']
    ans_win_rate = player_data['ans_wins'] / player_data['ans_games'] if player_data['ans_games'] > 0 else 0

    osp_losses = player_data['osp_games'] - player_data['osp_wins']
    osp_win_rate = player_data['osp_wins'] / player_data['osp_games'] if player_data['osp_games'] > 0 else 0

    best_friends = get_best_friends(player_data, database)
    best_friends_html = []
    for idx, friend in enumerate(best_friends, 1):
        best_friends_html.append(
            f'<tr>'
            f'<td>{idx}</td>'
            f'<td><a href="{friend["id"]}.html">{friend["name"]}</a></td>'
            f'<td>{friend["win_rate"]:.1%}</td>'
            f'<td>{friend["wins"]}/{friend["games"]}</td>'
            f'</tr>'
        )
    
    html_content = f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{player_data['steam_name']} - Player Stats</title>
    <style>
        body {{ 
            font-family: monospace;
            margin: 2rem;
            background-color: #1a1a1a;
            color: #e0e0e0;
        }}
        .header {{
            border-bottom: 2px solid #3a3a3a;
            margin-bottom: 1rem;
            padding-bottom: 1rem;
        }}
        .stats-table {{
            margin: 2rem 0;
            border-collapse: collapse;
            width: 100%;
            background-color: #2d2d2d;
            border-radius: 8px;
            overflow: hidden;
        }}
        .stats-table td, .stats-table th {{
            padding: 1rem;
            border: 1px solid #3a3a3a;
        }}
        .stats-table th {{
            background-color: #333333;
            color: #00cc99;
            font-weight: 600;
            width: 30%;
        }}
        .stats-table tr:nth-child(even) {{
            background-color: #262626;
        }}
        .stats-table tr:hover {{
            background-color: #363636;
            transition: background-color 0.2s ease;
        }}
        img {{
            max-width: 800px;
            margin: 2rem 0;
            border-radius: 4px;
        }}
        a {{
            color: #00ccff; 
            text-decoration: none;
            font-weight: 500;
        }}
        a:hover {{
            color: #00ffff;
            text-decoration: underline;
        }}
        h1, h2 {{
            color: #ffffff;
            margin: 0.5rem 0;
        }}
        .plot-container {{
            background-color: #2d2d2d;
            padding: 1rem;
            border-radius: 8px;
            margin: 2rem 0;
        }}
    </style>
</head>
<body>
    <div class="header">
        <img src="../dlo.webp" alt="Logo" width="150">
        <h1>{player_data['steam_name']} - Player Statistics</h1>
        <a href="https://openskill.me/en/stable/manual.html">ranking system info</a> 
        | <a href="../index.html">Player Leaderboard</a>
        | <a href="../rank_distribution.html">DLO Rank Distributions</a>
        | <a href="../match_history.html">Match History</a>
    </div>

    <table class="stats-table">
        <tr>
            <th>Steam Name</th>
            <td>{player_data['steam_name']}</td>
        </tr>
        <tr>
            <th>Player ID</th>
            <td>{player_id}</td>
        </tr>
        <tr>
            <th>Wins/Losses</th>
            <td>{player_data['wins']} / {losses} ({win_rate:.1%})</td>
        </tr>
        <tr>
            <th>Current DLO</th>
            <td>{player_data['rating_data'].ordinal():.2f}</td>
        </tr>
        <tr>
            <th>Mu (μ)</th>
            <td>{player_data['rating_data'].mu:.2f}</td>
        </tr>
        <tr>
            <th>Sigma (σ)</th>
            <td>{player_data['rating_data'].sigma:.2f}</td>
        </tr>
        <tr>
            <th>ANS Wins/Losses</th>
            <td>{player_data['ans_wins']} / {ans_losses} ({ans_win_rate:.1%})</td>
        </tr>
        <tr>
            <th>OSP Wins/Losses</th>
            <td>{player_data['osp_wins']} / {osp_losses} ({osp_win_rate:.1%})</td>
        </tr>
    </table>

    <h2>Top Teammates</h2>
    <table class="stats-table">
        <thead>
            <tr>
                <th>Rank</th>
                <th>Teammate</th>
                <th>Win Rate</th>
                <th>Record</th>
            </tr>
        </thead>
        <tbody>
            {"".join(best_friends_html)}
        </tbody>
    </table>

    <div class="plot-container">
        {open(plot_path).read()}
    </div>
</html>
    '''

    output_path = Path(f'docs/player/{player_id}.html')
    output_path.write_text(html_content)

def plot_rank_distribution(database: Dict[str, PlayerData], output_path: Path) -> None:
    ordinals = [p['rating_data'].ordinal() for p in database.values() if p['player']]
    
    if not ordinals:
        print("No player data available for rank distribution")
        return

    fig = go.Figure()
    
    fig.add_trace(
        go.Histogram(
            x=ordinals,
            name='Players',
            nbinsx=50,
            marker_color='#2ecc71',
            opacity=0.85,
    )
        )

    mean_val = sum(ordinals) / len(ordinals)
    median_val = sorted(ordinals)[len(ordinals)//2]

    fig.add_vline(
        x=mean_val, 
        line=dict(color='#e74c3c', width=2, dash='dash'),
        annotation=dict(text=f"Mean: {mean_val:.1f}", 
                       font=dict(color='#e74c3c'))
    )
    fig.add_vline(
        x=median_val, 
        line=dict(color='#3498db', width=2, dash='dash'),
        annotation=dict(text=f"Median: {median_val:.1f}", 
                       font=dict(color='#3498db'))
    )

    fig.update_layout(
        title='Player Rank Distribution',
        xaxis_title='DLO Rating',
        yaxis_title='Player Count',
        plot_bgcolor='#2d2d2d',
        paper_bgcolor='#1a1a1a',
        font=dict(
            family='monospace',
            color='#e0e0e0',
            size=14
        ),
        title_font_size=18,
        hovermode='x unified',
        margin=dict(l=60, r=30, t=80, b=60),
        xaxis=dict(
            showgrid=True,
            gridcolor='#3a3a3a',
            tickfont=dict(color='#e0e0e0'),
            linecolor='#3a3a3a',
            zeroline=False
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor='#3a3a3a',
            tickfont=dict(color='#e0e0e0'),
            linecolor='#3a3a3a',
            zeroline=False
        ),
        hoverlabel=dict(
            bgcolor='#333333',
            font_size=12,
            font_family='monospace'
        ),
        legend=dict(
            bgcolor='#2d2d2d',
            font=dict(color='#e0e0e0')
        )
    )

    fig.write_html(
        output_path,
        include_plotlyjs='cdn',
        full_html=True,
        config={'displayModeBar': False}  # Cleaner display
    )

def generate_dlo_plot(
    player_data: PlayerData,
    output_path: Path
) -> None:
    history = player_data['history']
    if not history:
        return

    times, ordinals = zip(*sorted(history, key=lambda x: x[0]))
    
    fig = px.line(
        x=times,
        y=ordinals,
        markers=True,
        labels={'x': 'Date', 'y': 'DLO'},
        title=f'DLO Rating History - {player_data["steam_name"]}'
    )
    
    fig.update_layout(
        plot_bgcolor='#2d2d2d',
        paper_bgcolor='#1a1a1a',
        font=dict(
            family='monospace',
            color='#e0e0e0'
        ),
        title=dict(
            font=dict(
                color='#ffffff'
            )
        ),
        hovermode='x unified',
        margin=dict(l=40, r=40, t=60, b=40),
        xaxis=dict(
            showgrid=True,
            gridcolor='#3a3a3a',
            tickfont=dict(color='#e0e0e0'),
            tickformat='%Y-%m-%d',
            linecolor='#3a3a3a'
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor='#3a3a3a',
            tickfont=dict(color='#e0e0e0'),
            linecolor='#3a3a3a'
        ),
        hoverlabel=dict(
            bgcolor='#333333',
            font_size=12,
            font_family='monospace',
            font_color='#e0e0e0'
        )
    )
    
    fig.update_traces(
        line=dict(color='#00ccff', width=2),
        marker=dict(size=6, color='#00ccff'),
        hovertemplate=(
            '<span style="color:#e0e0e0">'
            '<b>%{x|%Y-%m-%d}</b><br>DLO: %{y:.2f}'
            '</span><extra></extra>'
        )
    )
    
    pio.write_html(
        fig,
        file=output_path,
        full_html=False,
        include_plotlyjs='cdn',
        default_width='100%',
        default_height='400px'
    )


def get_best_friends(player_data: PlayerData, database: Dict[str, PlayerData]) -> List[Dict[str, Any]]:
    teammates = []
    for teammate_id, stats in player_data['teammates'].items():
        # Skip teammates with zero wins
        if stats['wins'] == 0:
            continue
            
        if teammate_id in database:
            win_rate = stats['wins'] / stats['games']
            teammates.append({
                'id': teammate_id,
                'name': database[teammate_id]['steam_name'],
                'games': stats['games'],
                'wins': stats['wins'],
                'win_rate': win_rate
            })
    
    # Sort by win rate (descending), then games played (descending)
    sorted_teammates = sorted(teammates, 
                            key=lambda x: (-x['wins'], -x['win_rate']))
    
    return sorted_teammates[:3]  # Return top 3 (or fewer if less available)

def render_match_history(
    match_history: List[MatchData],
) -> None:
    """Display sorted leaderboard and generate static HTML"""
    sorted_match_history = sorted(match_history, 
                        key=lambda d: d["time"], 
                        reverse=True)

    # Generate individual match pages
    for match_data in sorted_match_history:
        render_match_details(match_data)
    
    # match history HTML
    html_content = f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DLO Leaderboard</title>
    <style>
        body {{
            font-family: monospace;
            margin: 2rem;
            background-color: #1a1a1a;
            color: #e0e0e0;
        }}
        .header {{
            border-bottom: 2px solid #3a3a3a;
            margin-bottom: 1rem;
            padding-bottom: 1rem;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background-color: #2d2d2d;
            border-radius: 8px;
            overflow: hidden;
        }}
        th, td {{
            padding: 0.75rem 1rem;
            border: 1px solid #3a3a3a;
            text-align: left;
        }}
        th {{
            background-color: #333333;
            color: #00cc99;
            font-weight: 600;
        }}
        tr:nth-child(even) {{
            background-color: #262626;
        }}
        tr:hover {{
            background-color: #363636;
            transition: background-color 0.2s ease;
        }}
        a {{
            color: #00ccff; 
            text-decoration: none;
            font-weight: 500;
        }}
        a:hover {{
            color: #00ffff;
            text-decoration: underline;
        }}
        h1 {{
            color: #ffffff;
            margin: 0.5rem 0;
        }}
    </style>
</head>
<body>
    <div class="header">
        <img src="dlo.webp" alt="Logo" width="150">
        <h1>Match History</h1>
        <a href="https://openskill.me/en/stable/manual.html">Ranking System Info</a> 
        | <a href="index.html">Player Leaderboard</a>
        | <a href="rank_distribution.html">DLO Rank Distributions</a>
        | <a href="match_history.html">Match History</a>
    </div>
    
    <table>
        <thead>
            <tr>
                <th>Date</th>
                <th>Average DLO</th>
                <th>Match Quality</th>
            </tr>
        </thead>
        <tbody>
            {"".join(
                f'<tr><td><a href="match/{m["time"].strftime("%Y%m%d_%H%M%S")}.html">{str(m["time"])}</a></td>'
                f'<td>{m["avg_dlo"]:0.2f}</td>'
                f'<td>{m["match_quality"]:0.2f}</td></tr>'
                for m in sorted_match_history
            )}
        </tbody>
    </table>
</body>
</html>
        '''

    output_path = Path('docs/match_history.html')
    output_path.write_text(html_content)
    print(f"\nGenerated match history")

def render_match_details(match_data: MatchData) -> None:
    """Generate an HTML page with detailed match information"""
    teams = match_data["teams"]
    winning_team = match_data["winning_team"]
    
    html_content = f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Match Details - {match_data["time"]}</title>
    <style>
        body {{
            font-family: monospace;
            margin: 2rem;
            background-color: #1a1a1a;
            color: #e0e0e0;
        }}
        .header {{
            border-bottom: 2px solid #3a3a3a;
            margin-bottom: 1rem;
            padding-bottom: 1rem;
        }}
        a {{
            color: #00ccff; 
            text-decoration: none;
            font-weight: 500;
        }}
        a:hover {{
            color: #00ffff;
            text-decoration: underline;
        }}
        h1, h2 {{
            color: #ffffff;
            margin: 0.5rem 0;
        }}
        
        /* Match details specific styles */
        .stats-table {{
            margin: 2rem 0;
            border-collapse: collapse;
            width: 100%;
            background-color: #2d2d2d;
            border-radius: 8px;
            overflow: hidden;
        }}
        .stats-table td, .stats-table th {{
            padding: 1rem;
            border: 1px solid #3a3a3a;
        }}
        .stats-table th {{
            background-color: #333333;
            color: #00cc99;
            font-weight: 600;
            width: 30%;
        }}
        .stats-table tr:nth-child(even) {{
            background-color: #262626;
        }}
        .stats-table tr:hover {{
            background-color: #363636;
            transition: background-color 0.2s ease;
        }}
        .teams {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 2rem;
        }}
        .team {{
            background-color: #2d2d2d;
            padding: 1rem;
            border-radius: 8px;
            border: 1px solid #3a3a3a;
        }}
        .player {{
            margin-bottom: 1rem;
            padding: 1rem;
            background-color: #262626;
            border-radius: 4px;
        }}
        .fleet-image {{
            max-width: 200px;
            height: auto;
            border: 1px solid #3a3a3a;
            border-radius: 4px;
            cursor: zoom-in;
        }}
        .fleet-image-modal {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0,0,0,0.8);
            justify-content: center;
            align-items: center;
            z-index: 1000;
        }}
        .fleet-image-modal img {{
            max-width: 90%;
            max-height: 90%;
            height: auto;
            border-radius: 4px;
        }}
        .close {{
            position: absolute;
            top: 1rem;
            right: 1rem;
            color: #e0e0e0;
            cursor: pointer;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Match Details - {match_data["time"]}</h1>
        <a href="../index.html">Player Leaderboard</a> 
        | <a href="../rank_distribution.html">DLO Rank Distributions</a>
        | <a href="../match_history.html">Match History</a>
    </div>

    <h2>Match Statistics</h2>
    <table class="stats-table">
        <tr>
            <th>Average DLO</th>
            <td>{match_data["avg_dlo"]:0.2f}</td>
        </tr>
        <tr>
            <th>Match Quality</th>
            <td>{match_data["match_quality"]:0.2f}</td>
        </tr>
        <tr>
            <th>Map</th>
            <td>Not Recorded</td>
        </tr>
    </table>

    <div class="teams">
        <!-- Team A -->
        <div class="team">
            <h2>{winning_team == "TeamA" and "Winning Team" or "Losing Team"}</h2>
            {"".join(
            f'<div class="player">'f'<h3><a href="../player/{player["player_id"]}.html">{player["steam_name"]} ({player["faction"]})</a></h3>'
            f'Fleet: '
            f'</div>'
                for player in teams["TeamA"]
            )}
        </div>

        <!-- Team B -->
        <div class="team">
            <h2>{winning_team == "TeamB" and "Winning Team" or "Losing Team"}</h2>
            {"".join(
                f'<div class="player">'f'<h3><a href="../player/{player["player_id"]}.html">{player["steam_name"]} ({player["faction"]})</a></h3>'
                f'Fleet: '
                f'</div>'
                for player in teams["TeamB"]
            )}
        </div>
    </div>

    <!-- Fleet Image Modal -->
    <div id="fleetImageModal" class="fleet-image-modal">
        <span class="close" onclick="closeImageModal()">&times;</span>
        <img id="modalImage" src="">
    </div>

    <script>
        function openImageModal(img) {{
            document.getElementById("fleetImageModal").style.display = "flex";
            document.getElementById("modalImage").src = img.src;
        }}
        
        function closeImageModal() {{
            document.getElementById("fleetImageModal").style.display = "none";
        }}
    </script>
</body>
</html>
'''

    output_path = Path(f'docs/match/{match_data["time"].strftime("%Y%m%d_%H%M%S")}.html')
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(html_content)
    print(f"Generated match details for {match_data['time']}")

def load_rank_adjustments(file_path: str = 'rank_adjustments.json') -> List[Dict[str, Any]]:
    """Load manual rating adjustments from JSON file"""
    try:
        with open(file_path, 'r') as f:
            adjustments = json.load(f)
        return adjustments
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in {file_path}")
        return []

def apply_manual_adjustments(
    model: PlackettLuce,
    database: Dict[str, PlayerData],
    adjustments: List[Dict[str, Any]]
) -> None:
    """Apply manual rating adjustments to players"""
    for adj in adjustments:
        steam_id = adj['steam_id']
        if steam_id in database:
            original = database[steam_id]['rating_data']
            
            adjusted_rating = model.rating(
                name = original.name,
                mu=original.mu + adj['mu_adjustment'],
                sigma=original.sigma
            )
            
            database[steam_id]['rating_data'] = adjusted_rating
            print(f"Adjusted {adj['steam_name']} ({steam_id}): "
                  f"μ {original.mu:.2f} → {adjusted_rating.mu:.2f} "
                  f"({adj['mu_adjustment']:+.2f}) - {adj['reason_for_adjustment']}")
        else:
            print(f"Warning: Player {adj['steam_name']} ({steam_id}) not found")

def parse_skirmish_report_datetime(filename: Path) -> datetime:
    formats = [
        "%d-%b-%Y %H-%M-%S",    # For filenames like "14-Apr-2025 22-30-01"
        "%Y-%m-%d %H:%M:%S.%f", # For strings like "2025-03-27 16:04:52.263218"
    ]
    datetime_str = filename.name.split(" - ")[-1].replace(".xml", "")

    for fmt in formats:
        try:
            return datetime.strptime(datetime_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Failed to parse datetime: {datetime_str}")

def main() -> None:
    model: PlackettLuce = PlackettLuce(balance=False, limit_sigma=False)
    database: Dict[str, PlayerData] = {}
    match_history: List[MatchData] = []
    
    # store new fleet files first
    collect_fleet_files()

    battle_reports = sorted(
        Path("/srv/steam/.steam/steam/steamapps/common/NEBULOUS Dedicated Server/Saves/SkirmishReports/").iterdir(),
        key=parse_skirmish_report_datetime)

    for index, file in enumerate(battle_reports):
        print(f"\nPROCESSING FILE: {file}")
        match_data = parse_battle_report(file)
        process_match_result(match_data, match_history, database, model)

    # Apply manual adjustments
    adjustments = load_rank_adjustments()
    if adjustments:
        print("\nApplying manual adjustments:")
        apply_manual_adjustments(model, database, adjustments)
    
    plot_rank_distribution(database, Path("docs/rank_distribution.html"))
    render_leaderboard(database)
    render_match_history(match_history)

if __name__ == "__main__":
    main()
