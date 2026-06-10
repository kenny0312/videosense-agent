import psycopg2, os

SPORTS_KEYWORDS = [
    'volleyball', 'basketball', 'soccer', 'football', 'baseball',
    'swimming', 'diving', 'climbing', 'snowboard', 'snow', 'ski',
    'wrestling', 'sumo', 'dodgeball', 'paintball', 'skateboard',
    'surf', 'windsurf', 'hurling', 'gymnastics', 'hammer throw',
    'javelin', 'shot put', 'jump rope', 'aerobic', 'horse', 'motocross',
    'martial', 'arm wrestling', 'baton', 'cheerleading', 'disc',
]

conn = psycopg2.connect(
    host=os.environ.get('ALLOYDB_HOST', 'localhost'),
    dbname=os.environ.get('ALLOYDB_DB', 'your_database'),
    user=os.environ.get('ALLOYDB_USER', 'postgres'),
    password=os.environ['ALLOYDB_PASSWORD'], sslmode='require'
)
cur = conn.cursor()

conditions = ' OR '.join([f"primary_activity ILIKE '%{k}%'" for k in SPORTS_KEYWORDS])
cur.execute(f'''
    SELECT video_id, primary_activity, scene, duration_estimate
    FROM video_discovery
    WHERE {conditions}
    ORDER BY primary_activity
''')

rows = cur.fetchall()
print(f'\n找到 {len(rows)} 个体育活动视频:\n')
print(f'  {"#":<4} {"视频ID":<22} {"主要活动":<48} {"场景":<10} {"时长"}')
print('  ' + '-'*95)
for i, r in enumerate(rows, 1):
    print(f'  {i:<4} {r[0]:<22} {r[1]:<48} {r[2]:<10} {r[3]:.0f}s')
conn.close()
