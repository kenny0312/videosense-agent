"""GD-2:评测世界 B 种子(与世界 A 完全隔离的第二考场)。

为什么要第二个世界(审计结论):世界 A 的 145 道金标全按"封闭世界恰好 16 个视频"标定
("正好 2 个做饭视频""没有游泳")—— 直接扩世界 A = 推翻全部旧金标重审。
所以:**世界 A 冻结,新题挂世界 B**。按题选世界(task.world = "B"),EvalBackend
重灌种子时切换,两个世界永不同场。

设计要点:
  · 刻意收录世界 A「库外」的实体(游泳/瑜伽/猫/吉他/足球/沙拉)→ 同款诚实题在
    B 里答案翻转为「有」—— 直接测优化器是否把「说没有」背成了模板(防背题探针);
  · id 一律 b0xx(与 v0xx/sky0x 零冲突 → FACT_SHEETS 可以合一张表);
  · 无跳伞纵深(skydive_segments 在 B 里为空表 —— 顺带测空表诚实)。

(video_id, title, gcs_uri, duration_sec, [activities ...])
"""
from __future__ import annotations

VIDEOS_B = [
    ("b001", "Freestyle Swimming Laps",        "gs://activitynet/b001.mp4", 55.0, ["swimming", "freestyle swimming"]),
    ("b002", "Morning Yoga Flow",              "gs://activitynet/b002.mp4", 70.0, ["yoga", "stretching"]),
    ("b003", "Cat Playing with Yarn",          "gs://activitynet/b003.mp4", 25.0, ["cat playing", "pet"]),
    ("b004", "Acoustic Guitar Lesson",         "gs://activitynet/b004.mp4", 80.0, ["playing guitar", "music lesson"]),
    ("b005", "Soccer Penalty Kicks",           "gs://activitynet/b005.mp4", 47.0, ["playing soccer", "kicking ball"]),
    ("b006", "Garden Salad Prep",              "gs://activitynet/b006.mp4", 40.0, ["making salad", "chopping vegetables"]),
    ("b007", "Sunset Beach Surfing",           "gs://activitynet/b007.mp4", 65.0, ["surfing", "ocean waves"]),
    ("b008", "Pottery Wheel Basics",           "gs://activitynet/b008.mp4", 90.0, ["making pottery", "shaping clay"]),
    ("b009", "City Drone Flyover",             "gs://activitynet/b009.mp4", 35.0, ["drone flying", "cityscape"]),
    ("b010", "Boxing Heavy Bag Workout",       "gs://activitynet/b010.mp4", 50.0, ["boxing", "punching bag"]),
    ("b011", "Rock Climbing Indoor Wall",      "gs://activitynet/b011.mp4", 62.0, ["rock climbing", "climbing"]),
    ("b012", "Latte Art Pouring",              "gs://activitynet/b012.mp4", 30.0, ["making coffee", "latte art"]),
    ("b013", "Kayaking River Rapids",          "gs://activitynet/b013.mp4", 58.0, ["kayaking", "paddling"]),
    ("b014", "Puppy Training Sit and Stay",    "gs://activitynet/b014.mp4", 44.0, ["dog training", "pet"]),
    ("b015", "Watercolor Landscape Painting",  "gs://activitynet/b015.mp4", 85.0, ["painting", "watercolor"]),
    ("b016", "Night Market Street Food Tour",  "gs://activitynet/b016.mp4", 72.0, ["street food", "eating"]),
    ("b017", "Marathon Finish Line Moments",   "gs://activitynet/b017.mp4", 38.0, ["running marathon", "running"]),
    ("b018", "Ice Skating Rink Spins",         "gs://activitynet/b018.mp4", 41.0, ["ice skating", "spinning"]),
    ("b019", "Home Gym Deadlift Form",         "gs://activitynet/b019.mp4", 33.0, ["weight lifting", "deadlift"]),
    ("b020", "Swimming Pool Diving Practice",  "gs://activitynet/b020.mp4", 29.0, ["diving", "swimming"]),
]

# (video_id, predicate, matched, confidence, rationale, start_ts, end_ts)
# 与世界 A 同表结构;每个视频 3-5 条,含时间段(timestamp 题金标来源)+ 少量 matched=0 反例。
FACTS_B = [
    ("b001", "swimming",          1, 0.96, "Swimmer doing freestyle laps in pool",        2.0, 52.0),
    ("b001", "freestyle stroke",  1, 0.90, "Clear freestyle arm-over-arm technique",      5.0, 48.0),
    ("b001", "flip turn",         1, 0.82, "Flip turn executed at pool wall",            24.0, 27.0),
    ("b001", "diving board",      0, 0.30, "No diving board visible in this clip",        0.0, 0.0),
    ("b002", "yoga",              1, 0.95, "Person moving through yoga flow on mat",      0.0, 70.0),
    ("b002", "downward dog pose", 1, 0.88, "Downward dog held mid-flow",                 18.0, 26.0),
    ("b002", "sun salutation",    1, 0.85, "Sun salutation sequence at start",            2.0, 15.0),
    ("b003", "cat playing",       1, 0.97, "Cat batting a ball of yarn",                  1.0, 23.0),
    ("b003", "yarn",              1, 0.92, "Red ball of yarn unraveling",                 0.0, 25.0),
    ("b003", "cat jumping",       1, 0.80, "Cat pounces on the yarn",                    12.0, 15.0),
    ("b004", "playing guitar",    1, 0.96, "Instructor strumming acoustic guitar",        3.0, 78.0),
    ("b004", "chord diagram",     1, 0.84, "On-screen chord diagram overlay",            20.0, 35.0),
    ("b004", "tuning guitar",     1, 0.78, "Guitar tuned before lesson",                  0.0, 12.0),
    ("b005", "playing soccer",    1, 0.95, "Players taking penalty kicks at goal",        0.0, 47.0),
    ("b005", "goalkeeper save",   1, 0.86, "Goalkeeper saves the second kick",           18.0, 22.0),
    ("b005", "scoring goal",      1, 0.90, "Ball hits top corner on final kick",         40.0, 44.0),
    ("b006", "making salad",      1, 0.94, "Chopping and tossing salad ingredients",      0.0, 40.0),
    ("b006", "chopping vegetables", 1, 0.92, "Cucumber and tomato chopped on board",      3.0, 22.0),
    ("b006", "salad dressing",    1, 0.83, "Olive oil dressing drizzled at end",         30.0, 37.0),
    ("b007", "surfing",           1, 0.95, "Surfer riding waves at sunset",               5.0, 60.0),
    ("b007", "wave riding",       1, 0.90, "Long ride along the wave face",              25.0, 45.0),
    ("b007", "paddling out",      1, 0.85, "Paddling through breakers at start",          0.0, 18.0),
    ("b008", "making pottery",    1, 0.96, "Clay centered and shaped on wheel",           5.0, 85.0),
    ("b008", "shaping clay",      1, 0.90, "Walls pulled up into bowl form",             30.0, 60.0),
    ("b008", "kiln",              0, 0.25, "Kiln firing not shown in this clip",          0.0, 0.0),
    ("b009", "drone flying",      1, 0.93, "Aerial drone shots over city skyline",        0.0, 35.0),
    ("b009", "skyscrapers",       1, 0.88, "Downtown skyscrapers from above",             8.0, 28.0),
    ("b010", "boxing",            1, 0.95, "Boxer working combinations on heavy bag",     2.0, 48.0),
    ("b010", "punching bag",      1, 0.94, "Heavy bag visibly swinging from hits",        2.0, 48.0),
    ("b010", "jump rope",         0, 0.20, "No jump rope segment in this clip",           0.0, 0.0),
    ("b011", "rock climbing",     1, 0.95, "Climber ascending indoor wall route",         3.0, 58.0),
    ("b011", "belaying",          1, 0.82, "Belayer managing rope at floor",              0.0, 62.0),
    ("b011", "reaching top",      1, 0.87, "Climber slaps top hold",                     52.0, 56.0),
    ("b012", "making coffee",     1, 0.94, "Espresso pulled and milk steamed",            0.0, 20.0),
    ("b012", "latte art",         1, 0.91, "Rosetta poured into cup",                    20.0, 28.0),
    ("b013", "kayaking",          1, 0.95, "Kayaker running river rapids",                2.0, 55.0),
    ("b013", "white water",       1, 0.89, "White water splashing over bow",             10.0, 40.0),
    ("b013", "eskimo roll",       1, 0.78, "Roll recovery after capsize",                33.0, 37.0),
    ("b014", "dog training",      1, 0.93, "Trainer teaching sit and stay commands",      0.0, 44.0),
    ("b014", "dog sitting",       1, 0.90, "Puppy sits on command",                       8.0, 12.0),
    ("b014", "treat reward",      1, 0.86, "Treat given after successful stay",          30.0, 33.0),
    ("b015", "painting",          1, 0.95, "Watercolor landscape painted on paper",       0.0, 85.0),
    ("b015", "mixing paint",      1, 0.84, "Colors mixed on palette",                     5.0, 15.0),
    ("b015", "mountain scene",    1, 0.88, "Mountain ridge emerges in painting",         40.0, 70.0),
    ("b016", "street food",       1, 0.93, "Vendor stalls with grilled skewers",          0.0, 72.0),
    ("b016", "eating",            1, 0.90, "Host tasting noodles and skewers",           25.0, 65.0),
    ("b016", "night market",      1, 0.92, "Lantern-lit night market crowd",              0.0, 72.0),
    ("b017", "running marathon",  1, 0.94, "Runners crossing marathon finish line",       0.0, 38.0),
    ("b017", "finish line",       1, 0.92, "Finish arch and timing clock visible",        5.0, 30.0),
    ("b017", "medal ceremony",    0, 0.35, "Medals not shown in this clip",               0.0, 0.0),
    ("b018", "ice skating",       1, 0.95, "Skater gliding and spinning on rink",         0.0, 41.0),
    ("b018", "spinning",          1, 0.89, "Multi-rotation spin at center ice",          20.0, 26.0),
    ("b019", "weight lifting",    1, 0.94, "Deadlift sets with barbell",                  0.0, 33.0),
    ("b019", "deadlift",          1, 0.93, "Conventional deadlift form demo",             4.0, 28.0),
    ("b020", "diving",            1, 0.93, "Divers practicing from springboard",          0.0, 29.0),
    ("b020", "swimming",          1, 0.85, "Swimming back to ladder after dives",        15.0, 26.0),
]
