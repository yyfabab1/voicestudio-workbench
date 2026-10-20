r"""
声音市场：搜别人**公开分享**的音色模型，挑中哪个下哪个，下完直接变成一个能用的角色。

------------------------------------------------------------------ 边界（写死在第一行，别动）
这里只做「搜索 + 下载 Hugging Face 上**公开可下载**的模型文件」，跟你在网页上
手动点 Download 是一回事。它不破解任何付费内容、不碰账号体系、不抓取非公开
资源；搜不到就是搜不到，不绕。

下载来的模型仍然遵循它自己的许可（HF 上每个仓库都标了 license）。本模块会把
许可原样显示在结果里 —— **怎么用是你自己的事**，商用前请自己看那个 license。

------------------------------------------------------------------ 为什么走 hf-mirror
本机实测（2026-10-12）：
  · `huggingface.co` 直连 → `Tunnel connection failed: 502`（连不上）
  · `hf-mirror.com`     → API 正常，**但必须带 UA**（不带 UA 直接 403）
  · 个别仓库的 LFS 文件在镜像上 GET 会超时（HEAD 却是通的）⇒ 下载必须能失败、
    能重试、能换源，且**失败时不留半截文件**（半截 .pth 加载时报的错看不懂）。

------------------------------------------------------------------ 为什么"下载完就建档"
别人训练好的 RVC 模型 = 一个 .pth（权重，几十 MB）+ 一个 .index（检索库，
几百 MB 可选）。过去要把它变成能用的角色，得自己改文件名、放进 RVC 的
assets/weights、再想办法让索引对上 —— 少做一步就是"模型加载失败"。
所以这里把落盘、改名、放对目录、建角色卡片一口气做完。
"""
import concurrent.futures
import difflib
import json
import os
import re
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CACHE = os.path.join(DATA, "modelhub_cache.json")
DOWNLOADS = os.path.join(DATA, "modelhub")

# 两个源依次试：镜像优先（本机直连 HF 不通），真身兜底（万一哪天镜像挂了）
HOSTS = ("https://hf-mirror.com", "https://huggingface.co")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 12        # 单次请求上限。★ 不能再大：一次查询最坏要试两个源 × 重试一次，
                    # 25 秒的话整条 HTTP 接口能被拖到 60 秒以上 ⇒ 用户点一下就"转圈半天"
                    # （实测 /api/models/detail 挂到 60+ 秒，不是报错，是超时）
FALLBACK_TIMEOUT = 8  # 第二个源（huggingface.co 直连）在本机基本不通，别让它拖全程
UA_HEADERS = {"User-Agent": UA, "Accept": "application/json"}


class _NoRepo(Exception):
    """仓库不存在 / 没公开（401、403、404）。

    ★ 这是**确定答案**，必须跟"网络失败"分开：实测 hf-mirror 对不存在的仓库
    返回 **401**（0.75 秒就回来了）。以前一律当网络错误 ⇒ 重试 3 次、再换源，
    而第二个源（huggingface.co）在本机要等 10 秒才失败 ⇒ 一个"查无此仓库"
    的请求被拖到 60 秒以上，界面上就是点了没反应。
    """

# 权重/索引文件的后缀。safetensors 也认（有些仓库只发这个），但 RVC 推理要的是
# .pth / .ckpt；safetensors 会在安装时明确告诉你"这个得先转格式"，不假装能用。
WEIGHT_EXT = (".pth", ".ckpt", ".safetensors")
PACK_EXT = (".zip", ".7z", ".rar")
INDEX_EXT = (".index",)

# ------------------------------------------------------------------ 作品预设
#
# 为什么要把角色名一个个写进来：HF 的搜索只能按**仓库名/标签**匹配，
# 而"小马宝莉"这四个字在英文仓库里根本不出现（他们叫 Twilight Sparkle、
# Rainbow Dash……）。只搜作品名只能捞到一两个合集仓库，搜角色名才能凑齐"全角色"。
# ★ 每个作品给的是**搜索关键词**，不是"保证有"的清单 —— 搜不到就是没有，
#   绝不编造结果（界面上也会照实显示"这个角色没搜到"）。
WORKS = [
    {"key": "mlp", "name": "小马宝莉",
     "alias": ["My Little Pony", "MLP", "Equestria Girls", "小马宝莉"],
     "chars": ["Twilight Sparkle", "Rainbow Dash", "Pinkie Pie", "Fluttershy",
               "Applejack", "Rarity", "Trixie", "Starlight Glimmer", "Discord",
               "Celestia", "Luna", "Apple Bloom", "Scootaloo", "Sweetie Belle",
               "Derpy", "Sunset Shimmer"]},
    {"key": "hazbin", "name": "地狱客栈",
     "alias": ["Hazbin Hotel", "地狱客栈", "Hazbin"],
     "chars": ["Alastor", "Charlie Morningstar", "Vaggie", "Angel Dust", "Husk",
               "Niffty", "Lucifer Morningstar", "Lilith", "Adam", "Emily",
               "Vox", "Valentino", "Velvette", "Sir Pentious", "Rosie"]},
    {"key": "helluva", "name": "极恶老大",
     "alias": ["Helluva Boss", "极恶老大"],
     "chars": ["Blitzo", "Stolas", "Moxxie", "Millie", "Loona", "Fizzarolli",
               "Verosika", "Octavia", "Vortex"]},
    {"key": "genshin", "name": "原神",
     "alias": ["Genshin Impact", "Genshin", "原神"],
     "chars": ["Paimon", "Hu Tao", "Furina", "Nahida", "Raiden Shogun", "Yae Miko",
               "Zhongli", "Venti", "Keqing", "Eula", "Ayaka", "Kazuha"]},
    {"key": "hsr", "name": "崩坏：星穹铁道",
     "alias": ["Honkai Star Rail", "Star Rail", "星穹铁道"],
     "chars": ["Kafka", "Silver Wolf", "Seele", "Bronya", "Jingliu", "Fu Xuan",
               "Firefly", "Robin", "Ruan Mei", "Blade"]},
    {"key": "arknights", "name": "明日方舟",
     "alias": ["Arknights", "明日方舟"],
     "chars": ["Amiya", "Lappland", "Rosmontis", "Texas", "Exusiai", "Skadi",
               "Surtr", "Mostima", "Ch'en", "Kal'tsit"]},
    {"key": "vocaloid", "name": "VOCALOID / 初音",
     "alias": ["Hatsune Miku", "Vocaloid", "初音未来", "Kagamine Rin", "Megurine Luka"],
     "chars": ["Hatsune Miku", "Kagamine Rin", "Kagamine Len", "Megurine Luka",
               "KAITO", "MEIKO", "GUMI", "IA"]},
    {"key": "disney", "name": "迪士尼公主",
     "alias": ["Disney Princess", "Disney"],
     "chars": ["Elsa", "Anna", "Ariel", "Belle", "Moana", "Rapunzel", "Mulan",
               "Cinderella", "Snow White", "Jasmine", "Tiana", "Merida"]},
    {"key": "spongebob", "name": "海绵宝宝",
     "alias": ["SpongeBob", "Spongebob Squarepants", "海绵宝宝"],
     "chars": ["SpongeBob", "Patrick", "Squidward", "Mr. Krabs", "Sandy",
               "Plankton", "Gary"]},
    {"key": "rwby", "name": "RWBY",
     "alias": ["RWBY"],
     "chars": ["Ruby Rose", "Weiss Schnee", "Blake Belladonna", "Yang Xiao Long",
               "Pyrrha Nikos", "Nora Valkyrie", "Jaune Arc"]},
    {"key": "mlp_eg", "name": "小马国女孩",
     "alias": ["Equestria Girls", "My Little Pony Equestria"],
     "chars": ["Sunset Shimmer", "Twilight Sparkle human", "Rainbow Dash EG"]},
    {"key": "anime_zh", "name": "国创 / 中文配音",
     "alias": ["中文配音", "Chinese dub", "国漫"],
     "chars": ["哪吒", "敖丙", "孙悟空", "李白", "王者荣耀"]},
    {"key": "jjk", "name": "咒术回战",
     "alias": ["Jujutsu Kaisen", "JJK", "咒术回战"],
     "chars": ["Yuji Itadori", "Megumi Fushiguro", "Nobara Kugisaki",
               "Satoru Gojo", "Sukuna", "Kento Nanami", "Toge Inumaki",
               "Maki Zenin", "Panda"]},
    {"key": "demon", "name": "鬼灭之刃",
     "alias": ["Demon Slayer", "Kimetsu no Yaiba", "鬼灭之刃"],
     "chars": ["Tanjiro", "Nezuko", "Zenitsu", "Inosuke", "Giyu", "Rengoku",
               "Shinobu", "Muzan", "Akaza"]},
    {"key": "aot", "name": "进击的巨人",
     "alias": ["Attack on Titan", "Shingeki no Kyojin", "进击的巨人"],
     "chars": ["Eren Yeager", "Mikasa", "Armin", "Levi", "Historia",
               "Erwin", "Reiner", "Annie", "Hange"]},
    {"key": "spyfamily", "name": "间谍过家家",
     "alias": ["Spy x Family", "SPY FAMILY", "间谍过家家"],
     "chars": ["Anya Forger", "Loid Forger", "Yor Forger", "Bond Forger",
               "Damian", "Becky", "Yuri Briar"]},
    {"key": "chainsaw", "name": "电锯人",
     "alias": ["Chainsaw Man", "电锯人"],
     "chars": ["Denji", "Power", "Aki Hayakawa", "Makima", "Reze", "Kobeni",
               "Himeno"]},
    {"key": "naruto", "name": "火影忍者",
     "alias": ["Naruto", "火影忍者"],
     "chars": ["Naruto Uzumaki", "Sasuke Uchiha", "Sakura Haruno", "Kakashi",
               "Itachi", "Hinata", "Gaara", "Jiraiya", "Tsunade"]},
    {"key": "onepiece", "name": "海贼王",
     "alias": ["One Piece", "海贼王"],
     "chars": ["Luffy", "Zoro", "Nami", "Sanji", "Robin", "Chopper", "Usopp",
               "Franky", "Brook", "Boa Hancock"]},
    {"key": "eva", "name": "新世纪福音战士",
     "alias": ["Evangelion", "Neon Genesis Evangelion", "EVA"],
     "chars": ["Shinji", "Asuka", "Rei", "Misato", "Kaworu", "Mari", "Gendo"]},
    {"key": "uma", "name": "赛马娘",
     "alias": ["Uma Musume", "Pretty Derby", "赛马娘"],
     "chars": ["Special Week", "Silence Suzuka", "Tokai Teio", "Gold Ship",
               "Oguri Cap", "Haru Urara", "Mejiro McQueen", "Rice Shower"]},
    {"key": "bluearchive", "name": "蔚蓝档案",
     "alias": ["Blue Archive", "蔚蓝档案"],
     "chars": ["Arona", "Shiroko", "Nonomi", "Serika", "Ayane", "Hoshino",
               "Aru", "Karin"]},
    {"key": "azurlane", "name": "碧蓝航线",
     "alias": ["Azur Lane", "碧蓝航线"],
     "chars": ["Enterprise", "Belfast", "Prinz Eugen", "Akagi", "Kaga",
               "Atago", "Takao", "Laffey"]},
    {"key": "honkai3", "name": "崩坏 3",
     "alias": ["Honkai Impact", "Honkai Impact 3rd", "崩坏3"],
     "chars": ["Kiana", "Mei", "Bronya", "Fu Hua", "Raiden Mei", "Seele",
               "Elysia", "Theresa"]},
    {"key": "adventuretime", "name": "探险活宝",
     "alias": ["Adventure Time", "探险活宝"],
     "chars": ["Finn", "Jake", "Princess Bubblegum", "Marceline",
               "Ice King", "BMO"]},
    {"key": "rickmorty", "name": "瑞克和莫蒂",
     "alias": ["Rick and Morty", "瑞克和莫蒂"],
     "chars": ["Rick Sanchez", "Morty Smith", "Summer Smith", "Beth Smith",
               "Jerry Smith", "Birdperson"]},
    {"key": "simpsons", "name": "辛普森一家",
     "alias": ["The Simpsons", "辛普森"],
     "chars": ["Homer Simpson", "Marge Simpson", "Bart Simpson",
               "Lisa Simpson", "Maggie Simpson", "Ned Flanders",
               "Moe Szyslak", "Principal Skinner"]},
]


# ------------------------------------------------------------------ 中文名
#
# 为什么要这个：分享者清一色用英文写角色名（Alastor-Hazbin-hotel-ita.zip），
# 装进角色库之后卡片上就是一串英文，认不出是谁。
#
# ★ 三条硬规矩：
#   ① **纯本地字典**，不调任何翻译接口 —— 不联网、不花钱、不消耗积分，装一百个
#      角色也是零成本，离线也能用。
#   ② 只写**确定**的译名；拿不准的一律留空，宁可让卡片上写英文，也不编一个错的
#      （装错角色比名字难看严重得多）。
#   ③ 原名永远保留在角色简介里，用户随时能对着原名去核对。
CN = {
    # 小马宝莉（主线 + 大电影 + 小马国女孩）
    #   译名取自中文维基/官方引进版；拿不准的一个都不写，宁可走音译。
    "twilight sparkle": "紫悦", "rainbow dash": "云宝", "pinkie pie": "碧琪",
    "fluttershy": "柔柔", "applejack": "苹果嘉儿", "rarity": "珍奇",
    "pinkie": "碧琪",                       # 分享者常常只写 Pinkie，指的还是碧琪
    "trixie": "特丽克西", "starlight glimmer": "星光熠熠", "discord": "无序",
    "celestia": "塞拉斯蒂娅公主", "luna": "露娜公主", "apple bloom": "苹果丽丽",
    "scootaloo": "飞板璐", "sweetie belle": "甜贝儿", "derpy": "小呆",
    "sunset shimmer": "余晖烁烁", "spike": "穗龙",
    "princess cadance": "音韵公主", "cadance": "音韵公主",
    "shining armor": "闪耀盔甲", "flurry heart": "风雪之心",
    "queen chrysalis": "虫茧女王", "chrysalis": "虫茧女王",
    "king sombra": "森布拉大王", "sombra": "森布拉大王",
    "nightmare moon": "梦魇之月", "cozy glow": "和煦光流",
    "tirek": "提雷克", "lord tirek": "提雷克", "grogar": "格罗迦",
    "tempest shadow": "狂风将军", "storm king": "风暴大王",
    "zecora": "泽科拉", "big macintosh": "大麦克", "granny smith": "史密思婆婆",
    "maud pie": "石灰派", "cheese sandwich": "芝士三明治",
    "vinyl scratch": "维尼尔", "octavia melody": "奥塔薇娅",
    "lyra heartstrings": "莉拉", "bon bon": "邦邦",
    "diamond tiara": "珠玉冠冠", "silver spoon": "白银勺勺",
    "daring do": "朵苒敢", "starswirl": "白胡子星璇", "sunburst": "日光耀耀",
    "shining armour": "闪耀盔甲",
    # 小马国女孩·彩虹摇滚里的海妖（炫惑组合）
    "adagio dazzle": "阿达吉奥·达佐", "aria blaze": "阿里亚·布雷兹",
    "sonata dusk": "索纳塔·达斯克",
    # 地狱客栈
    "alastor": "阿拉斯托", "charlie morningstar": "夏莉·晨星", "charlie": "夏莉",
    "vaggie": "维姬", "angel dust": "天使尘", "husk": "哈斯克", "niffty": "妮芙蒂",
    "lucifer morningstar": "路西法", "lucifer": "路西法", "lilith": "莉莉丝",
    "adam": "亚当", "emily": "艾米丽", "vox": "沃克斯", "valentino": "瓦伦蒂诺",
    "velvette": "维薇特", "sir pentious": "潘特斯爵士", "rosie": "罗西",
    # 极恶老大
    "blitzo": "比利兹", "stolas": "斯托拉斯", "moxxie": "莫克西", "millie": "米莉",
    "loona": "露娜", "fizzarolli": "菲扎罗利", "verosika": "维罗西卡",
    "octavia": "奥克塔维亚", "vortex": "沃特克斯",
    # 原神
    "paimon": "派蒙", "hu tao": "胡桃", "furina": "芙宁娜", "nahida": "纳西妲",
    "raiden shogun": "雷电将军", "yae miko": "八重神子", "zhongli": "钟离",
    "venti": "温迪", "keqing": "刻晴", "eula": "优菈", "ayaka": "神里绫华",
    "kazuha": "枫原万叶",
    # 星穹铁道
    "kafka": "卡芙卡", "silver wolf": "银狼", "seele": "希儿", "bronya": "布洛妮娅",
    "jingliu": "镜流", "fu xuan": "符玄", "firefly": "流萤", "robin": "知更鸟",
    "ruan mei": "阮梅", "blade": "刃",
    # 明日方舟
    "amiya": "阿米娅", "lappland": "拉普兰德", "rosmontis": "迷迭香",
    "texas": "德克萨斯", "exusiai": "能天使", "skadi": "斯卡蒂", "surtr": "史尔特尔",
    "mostima": "莫斯提马", "kal'tsit": "凯尔希", "chen": "陈晖洁",
    # VOCALOID
    "hatsune miku": "初音未来", "miku": "初音未来", "kagamine rin": "镜音铃",
    "kagamine len": "镜音连", "megurine luka": "巡音流歌", "gumi": "GUMI",
    # 迪士尼
    "elsa": "艾莎", "anna": "安娜", "ariel": "爱丽儿", "belle": "贝儿",
    "moana": "莫阿娜", "rapunzel": "乐佩", "mulan": "花木兰",
    "cinderella": "仙杜瑞拉", "snow white": "白雪公主", "jasmine": "茉莉公主",
    "tiana": "蒂安娜", "merida": "梅莉达",
    # 海绵宝宝
    "spongebob": "海绵宝宝", "patrick": "派大星", "squidward": "章鱼哥",
    "krabs": "蟹老板", "sandy": "珊迪", "plankton": "痞老板", "gary": "小蜗",
    # RWBY
    "ruby rose": "露比·罗丝", "weiss schnee": "魏丝·雪倪",
    "blake belladonna": "布蕾克·贝拉多娜", "yang xiao long": "阳小龙",
    "pyrrha nikos": "皮拉·尼可丝", "nora valkyrie": "诺拉·瓦尔基里",
    "jaune arc": "强·亚克",
    # 咒术回战
    "yuji itadori": "虎杖悠仁", "megumi fushiguro": "伏黑惠",
    "nobara kugisaki": "钉崎野蔷薇", "satoru gojo": "五条悟", "gojo": "五条悟",
    "sukuna": "两面宿傩", "kento nanami": "七海建人", "toge inumaki": "狗卷棘",
    "maki zenin": "禅院真希", "panda": "熊猫",
    # 鬼灭之刃
    "tanjiro": "灶门炭治郎", "nezuko": "灶门祢豆子", "zenitsu": "我妻善逸",
    "inosuke": "嘴平伊之助", "giyu": "富冈义勇", "rengoku": "炼狱杏寿郎",
    "shinobu": "蝴蝶忍", "muzan": "鬼舞辻无惨", "akaza": "猗窝座",
    # 进击的巨人
    "eren yeager": "艾伦·耶格尔", "eren": "艾伦", "mikasa": "三笠",
    "armin": "阿明", "levi": "利威尔", "historia": "希丝特莉亚", "erwin": "埃尔文",
    "reiner": "莱纳", "annie": "阿尼", "hange": "韩吉",
    # 间谍过家家
    "anya forger": "阿尼亚", "anya": "阿尼亚", "loid forger": "劳埃德·福杰",
    "yor forger": "约尔·福杰", "bond forger": "邦德", "damian": "达米安",
    "becky": "贝姬", "yuri briar": "尤里·布莱尔",
    # 电锯人
    "denji": "淀治", "power": "帕瓦", "aki hayakawa": "早川秋", "makima": "玛奇玛",
    "reze": "蕾塞", "kobeni": "小比奈", "himeno": "姬野",
    # 火影忍者
    "naruto uzumaki": "漩涡鸣人", "naruto": "鸣人", "sasuke uchiha": "宇智波佐助",
    "sasuke": "佐助", "sakura haruno": "春野樱", "kakashi": "旗木卡卡西",
    "itachi": "宇智波鼬", "hinata": "日向雏田", "gaara": "我爱罗",
    "jiraiya": "自来也", "tsunade": "纲手",
    # 海贼王
    "luffy": "路飞", "zoro": "索隆", "nami": "娜美", "sanji": "山治",
    "robin": "罗宾", "chopper": "乔巴", "usopp": "乌索普", "franky": "弗兰奇",
    "brook": "布鲁克", "boa hancock": "波雅·汉库克",
    # 新世纪福音战士
    "shinji": "碇真嗣", "asuka": "明日香", "rei": "绫波丽", "misato": "葛城美里",
    "kaworu": "渚薰", "mari": "真希波", "gendo": "碇源堂",
    # 赛马娘
    "special week": "特别周", "silence suzuka": "沉默铃鹿", "tokai teio": "东海帝王",
    "gold ship": "黄金船", "oguri cap": "小栗帽", "haru urara": "春乌菈菈",
    "mejiro mcqueen": "目白麦昆", "rice shower": "米浴",
    # 蔚蓝档案
    "arona": "阿洛娜", "shiroko": "白子", "nonomi": "野乃美", "serika": "芹香",
    "ayane": "绫音", "hoshino": "星野", "karin": "花凛",
    # 碧蓝航线
    "enterprise": "企业", "belfast": "贝尔法斯特", "prinz eugen": "欧根亲王",
    "akagi": "赤城", "kaga": "加贺", "atago": "爱宕", "takao": "高雄",
    "laffey": "拉菲",
    # 崩坏 3
    "kiana": "琪亚娜", "mei": "芽衣", "fu hua": "符华", "elysia": "爱莉希雅",
    "theresa": "德丽莎",
    # 探险活宝
    "finn": "芬恩", "jake": "杰克", "princess bubblegum": "泡泡糖公主",
    "marceline": "玛瑟琳", "ice king": "冰霸王",
    # 瑞克和莫蒂
    "rick sanchez": "瑞克", "rick": "瑞克", "morty smith": "莫蒂", "morty": "莫蒂",
    "summer smith": "夏茉", "beth smith": "贝丝", "jerry smith": "杰瑞",
    # 辛普森一家
    "homer simpson": "荷马·辛普森", "marge simpson": "玛姬", "bart simpson": "巴特",
    "lisa simpson": "丽莎", "maggie simpson": "玛吉", "ned flanders": "内德",
    "skinner": "斯金纳校长",
    # 国创
    "nezha": "哪吒", "ao bing": "敖丙", "sun wukong": "孙悟空", "li bai": "李白",
    # 葬送的芙莉莲（★ 这一条正是当年被翻成「艾伦」的那个名字 —— 见 to_cn 注释）
    "frieren": "芙莉莲", "fern": "费伦", "stark": "休塔尔克",
    "himmel": "欣梅尔", "heiter": "海塔", "eisen": "艾泽",
    "utena": "欧蒂娜", "anthy": "姬宫安希",
    "reinhard": "莱因哈特", "subaru": "昴", "rem": "雷姆", "ram": "拉姆",
    "emilia": "爱蜜莉雅",
}


# ------------------------------------------------------- 音译兜底
#
# 为什么还需要这一层：字典再全也追不上别人上传的速度。2026-10-13 用户一口气装了
# 16 个小马宝莉，卡片上一半还是英文 —— 因为字典里没那条名字。而**人名**翻成中文
# 本来就是按读音译（Adagio Dazzle → 阿达吉奥·达佐），这一层做的正是这件事：
# 结果**只由这个词的拼写决定**，不带任何"它可能是谁"的判断 ⇒ 永远不可能把
# Frieren 说成「艾伦」（那是子串匹配才会犯的事故，见 to_cn 里的负对照）。
#
# 三条规矩：
#   ① 确定译名永远优先（上面的 CN 字典），音译只在字典也没有的时候兜底；
#   ② 不像人名的一律不动：带数字、"model / voice / test" 这类通用词、没有元音的
#      乱码串 ⇒ 返回空串，让调用方保留原文；
#   ③ 原名照旧写进简介（studio 那边做），用户一眼能对着原名核对。
_ROW = ("a", "e", "i", "o", "u", "ai", "ei", "ao", "ou")

# 声母 × 韵母 → 常用译名汉字（照《英语姓名译名手册》的取字习惯）
_CV = {
    "":   ("阿", "埃", "伊", "奥", "乌", "艾", "埃", "奥", "欧"),
    "b":  ("巴", "贝", "比", "波", "布", "白", "贝", "包", "博"),
    "c":  ("卡", "克", "西", "科", "库", "凯", "克", "考", "科"),
    "ch": ("查", "切", "奇", "乔", "楚", "柴", "切", "曹", "乔"),
    "d":  ("达", "德", "迪", "多", "杜", "代", "德", "道", "杜"),
    "f":  ("法", "菲", "菲", "福", "弗", "法", "菲", "福", "福"),
    "g":  ("加", "格", "吉", "戈", "古", "盖", "格", "高", "戈"),
    "h":  ("哈", "赫", "希", "霍", "胡", "海", "赫", "豪", "霍"),
    "j":  ("贾", "杰", "吉", "乔", "朱", "杰", "杰", "焦", "乔"),
    "k":  ("卡", "凯", "基", "科", "库", "凯", "凯", "考", "科"),
    "l":  ("拉", "尔", "利", "洛", "卢", "莱", "莱", "劳", "洛"),
    "m":  ("玛", "梅", "米", "莫", "姆", "麦", "梅", "茂", "莫"),
    "n":  ("娜", "内", "尼", "诺", "努", "奈", "内", "瑙", "诺"),
    "p":  ("帕", "佩", "皮", "波", "普", "派", "佩", "保", "波"),
    "q":  ("卡", "凯", "基", "科", "库", "凯", "凯", "考", "科"),
    "r":  ("拉", "尔", "里", "罗", "鲁", "莱", "雷", "劳", "罗"),
    "s":  ("萨", "塞", "斯", "索", "苏", "赛", "塞", "绍", "索"),
    "sh": ("沙", "谢", "希", "肖", "舒", "谢", "谢", "绍", "肖"),
    "t":  ("塔", "特", "蒂", "托", "图", "泰", "特", "陶", "托"),
    "th": ("萨", "塞", "西", "索", "苏", "赛", "塞", "绍", "索"),
    "v":  ("瓦", "维", "维", "沃", "武", "怀", "维", "沃", "沃"),
    "w":  ("瓦", "韦", "威", "沃", "伍", "怀", "韦", "沃", "沃"),
    "x":  ("克萨", "克塞", "克西", "克索", "克苏", "克赛", "克塞", "克索", "克索"),
    "y":  ("亚", "耶", "伊", "约", "尤", "亚", "耶", "尧", "约"),
    "z":  ("扎", "泽", "兹", "佐", "祖", "宰", "泽", "佐", "祖"),
}

# 一个辅音单独成音节时插什么元音：这是照中国人译名的老习惯来的 ——
# b/p/m/f/v/w 补 u（布 / 普 / 姆 / 弗），s/z 补舌尖元音（斯 / 兹），其余补 e（格 / 特 / 克）。
# 不这么分，"Storm" 会译成「西托姆」而不是「斯托姆」，"Glow" 会译成「吉洛夫」而不是「格洛」。
_DEFAULT_NUC = {
    "b": "u", "p": "u", "m": "u", "f": "u", "v": "u", "w": "u",
    "s": "i", "z": "i", "x": "i", "y": "i",
    "th": "i", "ch": "i", "sh": "i",
}
# 其余（c/k/g/h/d/t/l/r/n/j/q/v）默认补 e

# 词尾辅音：先认双字母组合（Mash 的 sh → 什），再认单字母
_FIN2 = {"sh": "什", "ch": "奇", "th": "斯", "ng": "英", "ck": "克",
         "nk": "克", "gh": "夫", "ph": "夫", "nn": "恩", "ll": "尔"}
_FIN = {
    "n": "恩", "m": "姆", "r": "尔", "l": "尔", "s": "斯",
    "t": "特", "d": "德", "k": "克", "g": "格", "b": "布", "p": "普",
    "f": "夫", "v": "夫", "z": "兹", "c": "克", "x": "克斯", "w": "夫",
    "y": "伊",
}

_VOWELS = "aeiouy"

# 元音串拆出来的**后续**音节用什么字。词首的 a 是「阿」（Alastor），
# 词尾跟在别的元音后面的 a 是「亚」（Octavia / Sophia / Maria / Leah）。
# 不分开的话 Octavia 会译成「奥克塔维阿」，结尾那个字怎么看都别扭。
_VOW_TAIL = {"a": "亚", "o": "奥", "u": "乌", "i": "伊", "e": "埃"}

# 看着就不像人名的词（走音译会把模型包的名字也翻成怪东西）
_NOT_A_NAME = {
    "model", "models", "voice", "voices", "vocal", "vocals", "singer", "singers",
    "rvc", "ckpt", "weights", "weight", "checkpoint", "test", "tests", "demo",
    "demos", "pack", "packs", "packs organized", "character", "characters",
    "the", "and", "my", "new", "old", "final", "official", "sample", "samples",
    "dataset", "train", "training", "song", "songs", "audio", "sound", "mix",
    "cover", "chorus", "talking", "speaking", "library", "custom", "best",
    "talk", "sing", "singing", "voicebank", "vb", "talkbank", "darling",
}


def _nuc_key(nuc):
    """把一串元音归到表里那 9 个韵母之一。"""
    n = (nuc or "").lower()
    if n in ("ai", "ay"):
        return "ai"
    if n in ("ei", "ey", "eigh"):
        return "ei"
    if n in ("au", "aw"):
        return "ao"
    if n in ("ea", "ie", "eo"):
        return "ei"
    if n in ("oo", "ou", "ow", "oa", "oe", "eu", "ew", "ue"):
        return "ou"
    return n[0] if n[:1] in ("a", "e", "i", "o", "u") else "i"


# 认识的复合元音。不在这张表里的元音串（ia / oy）一律拆成两段分别成音节。
_NUC2 = {"ai", "ay", "ei", "ey", "au", "aw", "ea", "ie", "eo", "oo", "ou",
         "ow", "oa", "oe", "eu", "ew", "ue", "ee"}

# 词尾的 -ie 读 /i/，不是复合元音 ei：Rosie / Trixie / Charlie / Vaggie / Moxxie
# 全是这个名字尾。不先把那个 e 削掉，会译成「罗塞」「克哈尔莱」这种怪东西。
# 三个字母的词（pie / tie / die）本来就读 /ai/，靠这个长度门槛把它们排除掉。
_IE_MIN = 4


def _word_syllables(w):
    """把一个英文词切成 [(声母, 韵母, 词尾辅音)]。

    三处都是实测踩出来的坑，改之前先量过（见 _probe 时的输出）：
      · **复辅音要按最长声母取**。charlie 开头的 ch 明明在 _CV 里，原来一律
        "最后一个字母当声母、前面几个各补一个元音" ⇒ 译成「克哈…」。
      · **连续元音不能整团吞**。Octavia 的 ia 不在复合元音表里，原来只取头一个
        字母、剩下的**直接丢掉** ⇒「奥克塔维」，末尾那个 a 没了。现在只吃头一个，
        余下的**退回输入**当下一个音节 ⇒「奥克塔维亚」。
      · 词尾 -ie 先削（见 _IE_MIN）。
    """
    w = re.sub(r"(.)\1+", r"\1", w.lower().replace("'", ""))   # zz -> z
    if len(w) >= _IE_MIN and w.endswith("ie"):
        w = w[:-1]
    i, n = 0, len(w)
    out = []
    while i < n:
        cons = ""
        while i < n and w[i] not in _VOWELS:
            cons += w[i]
            i += 1
        if i >= n:
            # 词尾剩下的一串辅音：只留最后一个当尾音（storm 的 m → 姆）
            if cons:
                tail = cons[-2:] if cons[-2:] in _FIN2 else cons[-1]
                if out:
                    out[-1] = (out[-1][0], out[-1][1], tail)
                else:
                    out.append((cons[-1], "i", ""))
            break
        nuc = ""
        while i < n and w[i] in _VOWELS:
            nuc += w[i]
            i += 1
        if len(nuc) > 1 and nuc not in _NUC2:
            # 不认识的元音串：只吃第一个，剩下的退回去单独成音节
            i -= (len(nuc) - 1)
            nuc = nuc[:1]
        if len(cons) > 1:
            # 复辅音：先试"最后两个字母"是不是一个声母（ch / sh / th），
            # 不是才退成单字母；前面多出来的各按习惯元音单独成音节
            use = cons[-2:] if cons[-2:] in _CV else cons[-1]
            for c in cons[:-len(use)]:
                out.append((c, _DEFAULT_NUC.get(c, "e"), ""))
            cons = use
        out.append((cons, nuc, ""))
    return out


def _words_of(s):
    """把 "Pinkie Pie" / "rainbow_dash" / "CarmillaCarmine" 都切成单词表。

    驼峰连写必须在这里就切开：以前 "CarmillaCarmine" 被当成**一个 15 字母的单词**，
    超过 _looks_like_name 的 12 字母上限直接判"不像人名" ⇒ 整个名字返回空串。
    """
    parts = [p for p in re.split(r"[\s_]+", str(s or "").strip()) if p]
    out = []
    for p in parts:
        if re.search(r"[a-z][A-Z]", p):
            segs = re.findall(r"[A-Z][a-z0-9']+|[a-z0-9']+", p)
            out += segs if segs else [p]
        else:
            out.append(p)
    return out


def _looks_like_name(s):
    """这个字符串看着像一个人名/角色名吗。不敢断定就返回 False（保留英文）。"""
    s = (s or "").strip()
    if not s or len(s) > 28:
        return False
    if re.search(r"\d", s):                      # 带数字多半是 v2 / 81034 之类
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z' ]*", s):
        return False
    words = _words_of(s)
    if not (1 <= len(words) <= 3):
        return False
    _BAD_SUB = ("model", "voice", "vocal", "checkpoint", "ckpt", "weight",
                "sample", "dataset", "training", "speaker", "dataset",
                "mixdown", "instrumental", "master", "acapella")
    for w in words:
        lw = w.lower()
        if lw in _NOT_A_NAME or not (2 <= len(w) <= 12):
            return False
        if any(b in lw for b in _BAD_SUB):        # voicemodel 这种连写的也要挡住
            return False
        if not re.search(r"[aeiouy]", lw):        # 没有元音 ⇒ 八成是乱码
            return False
    return True


def transliterate(name):
    """英文角色名 → 按读音拼出来的中文。它只看拼写，不判断"这是谁"。"""
    if not _looks_like_name(name):
        return ""
    outs = []
    for w in _words_of(name):
        syls = _word_syllables(w)
        word = ""
        for si, (onset, nuc, tail) in enumerate(syls):
            row = _CV.get(onset)
            if not row:
                return ""
            k = _nuc_key(nuc)
            if k not in _ROW:
                k = "i"
            last = (si == len(syls) - 1)
            if not onset and si:
                # 元音串拆出来的后续音节：玛丽亚的「亚」，不是「阿」
                ch = _VOW_TAIL.get(nuc[:1], row[_ROW.index(k)])
            elif last and onset == "s" and k == "i":
                # 词尾的 -sie / -sy 读 /si/：Rosie 罗西、Lucy 露西、Daisy 黛西。
                # 表里的 s×i 是「斯」，那是 Williams 那种**词尾辅音**用的。
                ch = "西"
            else:
                ch = row[_ROW.index(k)]
            word += ch
            if tail:
                word += _FIN2.get(tail) or _FIN.get(tail, "")
        if not word:
            return ""
        outs.append(word)
    return "·".join(outs)


_VER_TAIL = re.compile(
    r"^(v\d+|\d+(k)?|final|fix\d*|ver\d+|new|old|latest|official|remaster(ed)?|"
    r"[a-z]?\d+|f\d+|e\d+(_s\d+)?|step\d+.*|testdemo.*|demo.*|test.*|opus.*)$")


def _head_variants(src):
    """把名字尾部那些版本尾巴一层层剥掉，逐个给字典试。

    "adagio dazzle v2" / "twilight sparkle 2" 这类带货后缀的写法，不剥就永远
    查不到字典里那条 "adagio dazzle"。先给完整的，再给剥掉一层的，
    最多剥两层（剥太多会变成另一个角色：也不要）。
    """
    seen, out = set(), []
    cur = (src or "").strip()
    for _ in range(3):
        if not cur or cur in seen:
            break
        seen.add(cur)
        out.append(cur)
        parts = cur.split(" ")
        if len(parts) > 1 and _VER_TAIL.match(parts[-1]):
            cur = " ".join(parts[:-1])
        else:
            break
    return out


def _forms(name):
    """把一个名字摊成几种候选写法，**越靠前越像正主**。

      flat  = 空格形态（rainbow_dash_g3 -> rainbow dash g3，下划线/短横都当分词符）
      camel = 驼峰还原成空格（TwilightSparkle -> twilight sparkle）
      cands = 上面两种各自再逐层剥版本尾巴（adagio dazzle v2 -> adagio dazzle）
    """
    s = str(name or "")
    flat = re.sub(r"\s+", " ", re.sub(r"[_\-\.]+", " ", s)).strip().lower()
    segs = re.findall(r"[A-Z][a-z0-9']+|[a-z0-9']+", s)
    camel = " ".join(w.lower() for w in segs) if segs else flat
    cands = []
    for src in (flat, camel):
        cands += _head_variants(src)
    return flat, camel, segs, cands


def name_rank(name):
    """这个英文名"有多正"，返回 (命中深度, 带版本尾巴, 长度)，**只用来排序**。

    为什么要这个：同一个角色装了三份（AdagioDazzleV2 / AdagioDazzleV3 / 拼错的
    AddagioDazzle），谁该拿不带后缀的干净名？原来按角色表顺序先到先得，实测干净名
    发给了 V2 那个包、正主反倒被挂上后缀。现在按"谁更接近字典里的正写"排：
    字典直接命中(0) < 剥掉版本尾才命中(1) < 只能靠 difflib 纠错(10+) < 只能音译(20)。
    """
    flat, camel, segs, cands = _forms(name)
    for i, cand in enumerate(cands):
        if cand in CN:
            return (i, 0, len(cand))
    for i, cand in enumerate(cands):
        if len(cand) < 6:
            continue
        near = difflib.get_close_matches(cand, CN, n=1, cutoff=0.90)
        if near and abs(len(near[0]) - len(cand)) <= 2:
            return (10 + i, 0, len(cand))
    tail = 1 if _VER_TAIL.match((flat.split(" ") or [""])[-1]) else 0
    return (20, tail, len(flat))


def to_cn(name):
    """英文角色名 → 中文名。**查不到返回空串**，让调用方自己决定退回英文。

    ★★ 这里踩过一次很难看的：最早用的是"整串包含"，于是 `Frieren`
    （葬送的芙莉莲）里因为有 `eren` 子串，被翻成了「艾伦」—— 那是进击的巨人里
    完全不相干的角色。用户看到一个**像是真的、其实是错的**中文名，比看到英文糟得多。

    所以字典匹配一律**按段**，三段式：
      ① 整个名字精确命中（"Alastor"、"tokai teio"）；
      ② 多词名按词边界匹配（"tokai teio" 出现在 "tokai teio rvc" 里）；
      ③ 单词名必须**自己就是独立的一段**（"SatoruGojo" 按驼峰切成 satoru / gojo，
         gojo 是独立段 ⇒ 命中；而 "Frieren" 切成 ["frieren"]，里面**没有**独立的
         eren ⇒ 不命中）。

    2026-10-13 又补了两件事：
      · **驼峰形的多词名以前整批漏**：字典写的是 "twilight sparkle"（空格），
        而分享者的文件名是 "TwilightSparkle"（连写）⇒ flat 里没有空格，多词键永远
        命中不了，于是装出来一片英文。现在两种写法都算（见下面 camel）。
      · **尾部版本会被挡住**："AdagioDazzleV2" 最后那个 v2 要把 Fifth wheel 剔掉再查。

    字典查不到时，交给 transliterate() 按读音译一层（它自己会把"不像人名"的挡掉）；
    再不行返回空串，让调用方保留原文。
    """
    if not name:
        return ""
    s = str(name)
    flat, camel, segs, cands = _forms(name)
    for cand in cands:
        if cand in CN:
            return CN[cand]
    # ★ 拼错的写法（分享者把 AdagioDazzle 写成 AddagioDazzle）也要能救回来：
    #   本地 difflib 差一个字母地纠错，**够像才认**（0.90 且长度差 ≤2）。
    #   不像就不动 —— 这一步最容易制造"看着像真的其实是另一个角色"的事故。
    for cand in cands:
        if len(cand) < 6:
            continue
        near = difflib.get_close_matches(cand, CN, n=1, cutoff=0.90)
        if near and abs(len(near[0]) - len(cand)) <= 2:
            return CN[near[0]]

    words = set(w.lower() for w in segs)
    words |= set(re.findall(r"[a-z0-9']+", flat))
    best_k, best_v = "", ""
    for k, v in CN.items():
        if len(k) < 3:
            continue
        hit = False
        if " " in k:
            hit = (bool(re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(k), flat))
                   or bool(re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(k), camel)))
        else:
            hit = k in words
        if hit and len(k) > len(best_k):
            best_k, best_v = k, v
    if best_v:
        return best_v
    return transliterate(s)


def works():
    return [{"key": w["key"], "name": w["name"], "chars": len(w["chars"])} for w in WORKS]


def work_of(key):
    for w in WORKS:
        if w["key"] == key:
            return w
    return None


# ------------------------------------------------------------------ HTTP

def _get(url, timeout=TIMEOUT):
    """取一次。★ 网络抖动要重试、4xx 不要重试 —— 这两件事必须分开处理。

    hf-mirror 在**并发突发**下会随机 502/超时（实测一口气问 20 个仓库时能有一半
    拿不到）；不重试，一次抖动就直接变成"这个角色没搜到"，用户以为世上没有这个
    模型。但 401/403/404 是明确答复，重试和换源都改变不了结果，只能白白把请求
    时间翻好几倍。
    """
    last = None
    for i in range(2):                      # 最多重试一次：再多就是给用户添堵
        try:
            req = urllib.request.Request(url, headers=UA_HEADERS)
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise _NoRepo("HTTP %d" % e.code)
            last = e
            time.sleep(0.6 * (i + 1))
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def api(path, host=None):
    """调一次 API，两个源依次试。都失败才抛最后一个错。

    ★ 遇到 `_NoRepo`（仓库不存在/没公开）**立刻停**：换一个源去问"这个仓库存不
    存在"没有任何意义，只会让请求多等十几秒。
    """
    last = None
    for i, h in enumerate(((host,) if host else HOSTS)):
        try:
            return json.loads(_get(h + path, timeout=TIMEOUT if i == 0 else FALLBACK_TIMEOUT))
        except _NoRepo:
            raise
        except Exception as e:
            last = e
    raise last


def _file_kind(files):
    """这个仓库里有什么：RVC 权重 / SoVITS 权重 / 打包 / 索引。

    ★★ safetensors **不算**（这条踩过）：HF 上绝大多数 safetensors 仓库是
    LLM / 扩散模型，按"有权重文件"收录会把 tinybert、deit 这类全捞进来 ——
    实测搜角色名 "Adam" 时第一个结果是个情感分类模型。音色模型的权重后缀
    只有 .pth（RVC）/ .ckpt（SoVITS）/ .zip（打包），照这个收。
    """
    low = [f.lower() for f in files]
    kinds = []
    if any(f.endswith(".pth") for f in low):
        kinds.append("rvc")
    if any(f.endswith(".ckpt") for f in low):
        kinds.append("sovits")
    if any(f.endswith(PACK_EXT) for f in low):
        kinds.append("pack")
    return kinds


# 明显的"这不是音色模型"特征：见到就丢掉，别让用户在一堆 LLM 里找角色
_NOT_VOICE = ("tokenizer.json", "tokenizer_config.json", "vocab.txt",
              "model.onnx", "diffusers", "lora", ".gguf", ".bin",
              "special_tokens_map.json", "training_args.bin", "adapter_config.json")

# 看起来像音色模型的字样
_LIKE_VOICE = ("rvc", "svc", "sovits", "so-vits", "voice", "vocal", "sing",
               "tts", "applio", "weights", "音色", "歌声", "说话", "配音", "model")


def _looks_like_voice_model(repo_id, files):
    """像不像一个音色模型（不像就别往结果里塞）。

    为什么要这个判据：HF 的搜索是按**名字/标签**匹配的，搜角色名会把同名
    的 LLM、分类模型、图片模型全捞回来。文件清单里出现 tokenizer/vocab/gguf
    这些，就说明它不是我们要的东西。

    ★★ 为什么不能只认"名字里带 rvc/voice"（这条是改过的，原来就是这么写的）：
    实测自由搜 "Hazbin Hotel" 的 12 个候选**全是** Loren85 那批真正的角色模型，
    但一个都没进结果 —— 他们的仓库名只有「角色-作品-语言」，
    （Alastor-Hazbin-hotel-ita / Charlie-Hazbin-Hotel-Ita / Emily-Ita-300），
    压根不写 rvc、voice 这类字样，而权重是**一个 zip**。按旧规则全被当成脏东西
    丢掉，用户点名的"全角色"就是这么没的。而且它连安装都挡：
    detail() 也走这个判据 ⇒ 明明仓库在、文件在，界面上却说"读不到这个仓库"。

    真正的区分度在**文件后缀**上，不在名字上：LLM 用 safetensors / bin / gguf
    发权重（那几样在 `_file_kind` 那一步就已经不认了），RVC/SoVITS 用 .pth/.ckpt。
    """
    blob = (repo_id + " " + " ".join(files)).lower()
    for bad in _NOT_VOICE:
        if bad in blob:
            return False
    low = [f.lower() for f in files]
    # .pth / .ckpt = RVC / SoVITS 的原生权重后缀 ⇒ 强证据
    if any(f.endswith((".pth", ".ckpt")) for f in low):
        return True
    if any(h in blob for h in _LIKE_VOICE):
        return True
    # 只剩 .zip：后缀本身判不了（数据集也是 zip），但宁可让用户看见后自己判断 ——
    #   漏掉一个他要的角色，比多显示一张卡片贵得多。
    return any(f.endswith(PACK_EXT) for f in low)


# ★ 这一组是"音频模型，但**不是**音色克隆模型"：说话人识别 / 语种识别 / 语音识别 /
#   声码器 / TTS。它们同样发 .ckpt / .bin，宽松口径会放行，但拿来唱根本没用。
#   踩过：搜《地狱客栈》第二轮（单独搜角色名）把 speechbrain 的说话人识别和语种
#   识别模型放进了结果 —— 用户点进去装完才发现不会唱。
#   ★ 只匹配**仓库名**，不匹配文件名：RVC 仓库里常有 hubert / vocoder 之类的底模
#   文件，按文件名匹配会误杀一大片真模型。
_OTHER_AUDIO = ("spkrec", "ecapa", "voxceleb", "speaker-diarization",
                "speaker-recognition", "speaker-verification", "diarization",
                "lang-id", "language-identification", "voxlingua", "wav2vec",
                "whisper", "speech-recognition", "vocoder", "hifigan",
                "bigvgan", "text-to-speech", "voicebox", "seamless", "denoise")


def _is_other_audio_model(repo_id):
    low = (repo_id or "").lower()
    return any(b in low for b in _OTHER_AUDIO)


def _guess_char(name, work=None):
    """从仓库名/文件名里猜这是哪个角色（用于给结果标角色名，也用于算"缺谁"）。

    ★ 不能只拿**全名**比对（踩过）：角色清单写 "Charlie Morningstar"，
    仓库只写 Charlie-Hazbin-Hotel-Ita ⇒ 全名永远对不上，结果就是"明明搜到了，
    界面却说这个角色没模型"。所以多词名额外接受**其中任一段**（≥4 个字母，
    太短的字样会到处命中，比如 IA / Luna 这种宁可算没命中）。

    猜不出来就返回 None —— 界面上显示"未标注角色"，不硬猜一个错的。
    """
    if not work:
        return None
    hay = (name or "").lower()
    best, best_len = None, 0
    for c in work["chars"]:
        cl = c.lower()
        parts = cl.split()
        cands = [t for t in parts if len(t) >= 4]
        if len(parts) > 1:
            cands.append(cl)          # 多词名的全名命中更准，优先算它
        for cand in cands:
            if cand in hay and len(cand) > best_len:
                best, best_len = c, len(cand)
    return best


def _detail(repo, work=None, strict=True, stats=None):
    """取一个仓库的文件清单（失败/不是音色模型返回 None，不抛）。

    strict=True（自由搜索）：必须先"像音色模型"才收 —— 否则搜角色名会捞回一堆
    同名的 LLM。作品浏览时传 strict=False：查询词已经是「作品名 + 角色名」，
    命中本身就是证据，这时候再要求名字里带 rvc/voice 会把只写了角色名的
    仓库全漏掉（实测 Loren85 那批只写 "Alastor-Hazbin-hotel-ita"）。

    stats: 传一个 dict 进来会统计"几个是网络失败 / 几个真收下了"。★ 这个区分
    很重要：整批请求都被网络抖掉时，结果跟"世上真没有这个角色"长得一模一样，
    不统计就分不出来（详见 search() 里那段）。
    """
    try:
        d = api("/api/models/" + urllib.parse.quote(repo, safe="/"))
    except _NoRepo:
        # ★ 仓库不存在/没公开是**确定答案**，不是网络失败。记进 net 的后果：上层
        #   以为"再试一次说不定就有了"，还会把它谎报成"网络不通"（刚踩过 ——
        #   查一个不存在的仓库，界面显示"网络不通，稍后再点一次"，点一百次也没有）。
        return None
    except Exception:
        if stats is not None:
            stats["net"] = stats.get("net", 0) + 1
        return None
    files = [s.get("rfilename") for s in (d.get("siblings") or []) if s.get("rfilename")]
    kinds = _file_kind(files)
    if not kinds:
        return None
    if _is_other_audio_model(d.get("modelId") or repo):
        # 说话人识别/语种识别/声码器：宽松口径也别放进来（拿它唱不出来）
        return None
    if strict and not _looks_like_voice_model(d.get("modelId") or repo, files):
        return None
    if stats is not None:
        stats["ok"] = stats.get("ok", 0) + 1
    return {
        "repo": d.get("modelId") or repo,
        "name": (d.get("modelId") or repo).split("/")[-1],
        "author": (d.get("modelId") or repo).split("/")[0],
        "downloads": d.get("downloads") or 0,
        "likes": d.get("likes") or 0,
        "license": next((t.split(":", 1)[1] for t in (d.get("tags") or [])
                         if t.startswith("license:")), ""),
        "at": (d.get("lastModified") or "")[:10],
        "files": files[:40],
        "kinds": kinds,
        "packs": list_packs(files),
        "char": _guess_char((d.get("modelId") or repo), work),
        # 中文名（查不到就是空串，调用方自己退回英文 —— 不猜）
        "cn": to_cn(_guess_char((d.get("modelId") or repo), work)
                    or clean_name(d.get("modelId") or repo)),
        "size_hint": len(files),
    }


def _cache_get(key, ttl=300):
    try:
        d = json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        return None
    hit = d.get(key)
    if hit and time.time() - hit.get("_t", 0) < ttl:
        return hit.get("val")
    return None


def _cache_put(key, val):
    try:
        os.makedirs(DATA, exist_ok=True)
        try:
            d = json.load(open(CACHE, encoding="utf-8"))
        except Exception:
            d = {}
        d[key] = {"_t": time.time(), "val": val}
        # 只留最近 60 条：搜索结果会越攒越多，别让这个文件无限长
        if len(d) > 60:
            for k in sorted(d, key=lambda k: d[k].get("_t", 0))[:len(d) - 60]:
                d.pop(k, None)
        tmp = CACHE + ".tmp"
        json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, CACHE)
    except Exception:
        pass


# ------------------------------------------------------------------ 标签浏览
#
# ★ 这是"找到更多"的主力通道，比关键词搜索准得多：在 HF 上训音色模型的人大多会
#   给仓库打 `rvc` / `singing-voice-conversion` / `so-vits-svc` 这类标签，按标签
#   拉列表一次能拿 **300 个**仓库、还按下载量排好；而按关键词搜一次只捞得到十来个，
#   里面还混着一堆同名的 LLM（搜 "Adam" 第一结果是 tinybert，踩过）。
#   实测 filter=rvc 的池子抽查 38 个，37 个真带权重文件。
TAGS = (
    {"key": "rvc", "name": "RVC 音色模型", "tag": "rvc"},
    {"key": "svc", "name": "歌声转换 SVC", "tag": "singing-voice-conversion"},
    {"key": "sovits", "name": "So-VITS-SVC", "tag": "so-vits-svc"},
)


def tags():
    return [{"key": t["key"], "name": t["name"]} for t in TAGS]


def _by_tag(tag, limit=300):
    """按标签拉仓库清单（一次一个请求，几百个仓库）。

    ★ 只要清单，不要文件：文件清单得逐个仓库再问一次，300 个一气儿问会把镜像
    打挂（实测并发一大就随机 502）。这里只建**候选池**，文件等翻到那一页再问。
    """
    path = ("/api/models?filter=%s&sort=downloads&direction=-1&limit=%d"
            % (urllib.parse.quote(tag), limit))
    try:
        rows = api(path)
    except Exception:
        return []
    out, seen = [], set()
    for r in rows:
        rid = r.get("modelId") or r.get("id")
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


# ------------------------------------------------------------------ 结果池
#
# 翻页为什么要有池：不池化的话，第 2 页要么重新搜一遍（慢，而且可能跟第 1 页
# 撞车），要么搜两次拼起来（同样慢）。池里存"候选仓库的有序清单"，文件清单
# 只在**真要显示的那一页**上问，问完记回池里 —— 首屏不用为第 10 页付费，
# 翻回去也不用再问镜像一次。
_POOLS = {}
_POOL_TTL = 900      # 15 分钟没动静就作废（HF 上的东西会变，也别让内存一直涨）
_POOL_MAX = 24


def _pool_new(cands, kind, label, work_key=None):
    import uuid

    qid = uuid.uuid4().hex[:10]
    _POOLS[qid] = {"t": time.time(), "kind": kind, "label": label,
                   "cands": list(cands), "done": {}, "work": work_key}
    if len(_POOLS) > _POOL_MAX:
        for k in sorted(_POOLS, key=lambda k: _POOLS[k]["t"])[:len(_POOLS) - _POOL_MAX]:
            _POOLS.pop(k, None)
    return qid


def _pool_get(qid):
    p = _POOLS.get(qid or "")
    if not p:
        return None
    if time.time() - p["t"] > _POOL_TTL:
        _POOLS.pop(qid, None)
        return None
    p["t"] = time.time()          # 还在翻 ⇒ 续命
    return p


def page(qid, offset=0, limit=24):
    """翻一页。返回本页 items + 下一页该从哪开始（has_more=False 就是到底了）。

    ★ 只问"这一页"的文件清单。strict 口径跟 `search()` 一致：自由搜索要严
    （不然捞回一堆 LLM），作品浏览/标签浏览放宽（命中本身就是证据）。
    """
    p = _pool_get(qid)
    if not p:
        return {"items": [], "error": "这一页过期了（超过 15 分钟没动过），请重新搜一次"}
    cands, done = p["cands"], p["done"]
    work = work_of(p["work"]) if p.get("work") else None
    strict = (work is None) and p["kind"] == "search"
    try:
        limit = min(48, max(1, int(limit)))
    except Exception:
        limit = 24
    rank = {r: i for i, r in enumerate(cands)}
    want, todo = [], []
    idx = max(0, int(offset or 0))
    while idx < len(cands) and len(want) + len(todo) < limit:
        rid = cands[idx]
        idx += 1
        if rid in done:
            if done[rid]:
                want.append(done[rid])
            continue
        todo.append(rid)
    stats = {"net": 0, "ok": 0}
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            for rid, d in zip(todo, ex.map(
                    lambda r: _detail(r, work, strict=strict, stats=stats), todo)):
                done[rid] = d
                if d:
                    want.append(d)
    want.sort(key=lambda d: rank.get(d["repo"], 1 << 30))
    res = {"items": want, "offset": idx, "has_more": idx < len(cands),
           "total": len(cands)}
    # 这一页一个都没读到、而且全是网络失败 ⇒ 不许落成"没有这个模型"
    if todo and stats["net"] and not stats["ok"]:
        res["net_fail"] = True
        res["note"] = ("模型站这会儿连不上（这一页 %d 个仓库一个都没读到），稍后再点一次"
                       % stats["net"])
    return res


def browse(tag_key="rvc", limit=24):
    """按标签浏览热门（返回第一页 + qid，后面用 page 翻）。"""
    t = None
    for x in TAGS:
        if x["key"] == (tag_key or ""):
            t = x
            break
    if not t:
        return {"items": [], "error": "没有这个标签"}
    ck = "b|" + t["tag"]
    hit = _cache_get(ck, ttl=600)
    cands = hit if isinstance(hit, list) else None
    if not cands:
        cands = _by_tag(t["tag"])
        if cands:
            _cache_put(ck, cands)
    if not cands:
        return {"items": [], "tag": tag_key,
                "note": "没读到这个标签下的仓库（模型站这会儿可能不通），稍后再点一次"}
    qid = _pool_new(cands, "tag", t["name"])
    res = page(qid, 0, limit)
    res.update({"qid": qid, "tag": tag_key, "tag_name": t["name"],
                "note": "按标签「%s」列出 %d 个仓库（已按下载量排序）"
                        % (t["name"], len(cands)) if cands else ""})
    return res


def _search_one(q, limit=12):
    path = ("/api/models?search=%s&limit=%d&sort=downloads&direction=-1&full=false"
            % (urllib.parse.quote(q), limit))
    try:
        return api(path)
    except Exception:
        return []


def search(q, limit=24, work_key=None, pool=False):
    """搜索：先按关键词拿候选，再并发取每个仓库的文件清单做过滤。

    为什么要"搜完再逐个问文件"：HF 的搜索 API **不返回**仓库里有哪些文件，
    而"这个仓库到底有没有权重"是唯一重要的判据 —— 光按名字搜会捞回一堆
    LLM / 数据集 / 图片模型。逐个问一次（并发 4），换来的是结果里没有垃圾。

    pool=True 时不一次问完，改成**建池 + 只返回第一页**（给界面翻页用）。

    ★ 自由搜索时，如果原词捞得太少（< 8 个候选），会换个说法再搜一轮：
      「原词 + rvc」「原词 + voice model」。很多音色仓库名字里压根不写 rvc，
      只搜原词就永远见不到它们。放在"结果太少时才补"是为了不稀释 ——
      原词够多的时候硬塞扩展词，会把不相干的高下载量模型顶到前面。
    """
    q = (q or "").strip()
    if not q:
        return {"items": [], "note": "没给关键词"}
    ck = "s|%s|%s|%d" % (work_key or "", q.lower(), limit)
    hit = _cache_get(ck)
    if hit is not None and not pool:
        return hit
    work = work_of(work_key) if work_key else None

    def collect(qs):
        got, seen = [], set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            for rows in ex.map(lambda s: _search_one(s, limit=max(limit, 24)), qs):
                for row in rows or []:
                    rid = row.get("modelId") or row.get("id")
                    if rid and rid not in seen:
                        seen.add(rid)
                        got.append(rid)
        return got

    cands = collect([q])
    if work is None and len(cands) < 8:
        cands = collect([q] + ["%s %s" % (q, suf) for suf in ("rvc", "voice model")])

    if pool:
        qid = _pool_new(cands, "search", q, work_key)
        res = page(qid, 0, limit)
        res.update({"qid": qid, "query": q, "work": work_key})
        if not res.get("net_fail"):
            res["note"] = ("在 %d 个候选里，已按下载量排序" % len(cands)) if cands \
                else "没搜到相关仓库，换个关键词试试（英文角色名命中率高很多）"
        return res

    out = []
    strict = work is None
    # 并发 8 打镜像会抖（实测能抖掉一半），降到 4 并让 _get 自己重试
    stats = {"net": 0, "ok": 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for d in ex.map(lambda r: _detail(r, work, strict=strict, stats=stats),
                        cands[:max(limit * 2, 24)]):
            if d:
                out.append(d)
    # ★★ 全部候选都因为网络失败而没读到 ⇒ 这不是"没搜到"，是"没读到"。
    #    必须分开：① 提示写清楚，让用户再点一次；② **不写缓存** ——
    #    一次抖动要是被缓存 5 分钟，用户接下来点什么都显示"这个角色没模型"。
    if cands and stats["net"] and not stats["ok"]:
        return {"items": [], "query": q, "work": work_key, "net_fail": True,
                "note": "模型站这会儿连不上（问了 %d 个仓库一个都没读到），稍后再点一次"
                        % stats["net"]}
    out.sort(key=lambda d: (-d["downloads"], -d["likes"]))
    res = {"items": out[:limit], "query": q, "work": work_key,
           "note": ("搜到 %d 个含权重文件的仓库（已按下载量排序）" % len(out)) if out
                   else "没搜到含权重文件的仓库，换个关键词试试"}
    _cache_put(ck, res)
    return res


def _cands_of(qs, workers=4):
    """并发跑几个查询词，**只**拿候选仓库 id（不问文件清单），去重后返回。

    ★ 为什么要先把候选汇总去重、再统一问文件：一个仓库会被好几个查询词同时
    命中，"每个查询各问一遍"就会把同一个仓库问四五次（一部作品有二十来个查询词）。
    白问的这些请求还会把镜像打挂（并发一大就随机 502）。
    """
    out, seen = [], set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(lambda s: _search_one(s, limit=12), qs):
            for row in rows or []:
                rid = row.get("modelId") or row.get("id")
                if rid and rid not in seen:
                    seen.add(rid)
                    out.append(rid)
    return out


def _details(cands, work=None, strict=True):
    """并发问一批仓库的文件清单，收下合格的。返回 (items, stats)。"""
    items = []
    stats = {"net": 0, "ok": 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for d in ex.map(lambda r: _detail(r, work, strict=strict, stats=stats), cands):
            if d:
                items.append(d)
    return items, stats


def search_work(work_key, limit=40):
    """按作品搜：**两轮**。返回里带 `missing`（没搜到的角色，必须照实列）。

    第一轮（宽松）：作品名 + 「作品名 角色名」 —— 命中本身就是证据。
    第二轮（严格）：只对**第一轮没命中的角色**，单独搜角色名。

    ★★ 第二轮是实测逼出来的，不是拍脑袋：连着作品名搜 "Uma Musume Tokai Teio"
    一个结果都没有，单独搜 "Tokai Teio" 能出 2 个真模型 —— HF 的搜索是按**仓库名**
    匹配的，而分享者只写 `teio-rvc`，压根不写作品名。实测效果：
      赛马娘 0 → 6 个仓库、Rainbow Dash 0 → 7、Satoru Gojo 0 → 8、
      Starlight Glimmer 0 → 3、Special Week 0 → 2。
    第二轮必须用**严格**过滤：单独搜角色名会捞回一堆同名的 LLM（搜 "Enterprise"
    前三是 Llama / wav2vec / sd-vae）。宽松口径下这些会混进结果。
    """
    w = work_of(work_key)
    if not w:
        return {"items": [], "missing": [], "note": "没有这个作品"}
    ck = "w|" + work_key
    hit = _cache_get(ck, ttl=600)
    if hit is not None:
        return hit
    main = w["alias"][0]
    qs1 = list(w["alias"]) + ["%s %s" % (main, c) for c in w["chars"]]
    cands1 = _cands_of(qs1)
    items, st1 = _details(cands1, w, strict=False)

    def matched_of(rows):
        got = " ".join((it["repo"] + " " + " ".join(it.get("files") or [])).lower()
                       for it in rows)
        m = set()
        for it in rows:
            c = _guess_char(it["repo"] + " " + " ".join(it.get("files") or []), w)
            if c:
                m.add(c)
        # ★ missing 不能拿**全名**去比对（踩过）：仓库叫 Charlie-Hazbin-Hotel-Ita，
        #   角色清单写 "Charlie Morningstar"，全名比对永远不成立 ⇒ 明明搜到了却报没搜到。
        return set(c for c in w["chars"] if c in m or c.lower() in got)

    matched = matched_of(items)
    miss = [c for c in w["chars"] if c not in matched]
    if miss:
        cands2 = [r for r in _cands_of(miss) if r not in set(cands1)]
        if cands2:
            more, st2 = _details(cands2, w, strict=True)
            for it in more:
                it["hit_by"] = "按角色名单独搜"
            items += more
            st1["net"] += st2["net"]
            st1["ok"] += st2["ok"]
    # 一个都没读到、而且全是网络失败 ⇒ 不许落成"这个作品一个模型都没有"
    if (cands1 or items == []) and st1["net"] and not st1["ok"]:
        return {"items": [], "missing": [], "work": work_key, "work_name": w["name"],
                "net_fail": True,
                "note": "模型站这会儿连不上（《%s》的候选一个都没读到），稍后再点一次"
                        % w["name"]}
    items.sort(key=lambda d: (-d["downloads"], d["name"]))
    # 两轮都跑完再算"还缺谁"（上面的 miss 只是用来决定第二轮搜谁）
    missing = [c for c in w["chars"] if c not in matched_of(items)]
    res = {"items": items[:limit], "missing": missing, "work": work_key,
           "work_name": w["name"],
           "note": "《%s》：搜到 %d 个仓库，%d 个角色没搜到公开模型"
                   % (w["name"], len(items), len(missing)) if items
                   else "《%s》一个都没搜到" % w["name"]}
    _cache_put(ck, res)
    return res


def _fill_sizes(repo, packs, max_n=60):
    """给每个包填体积。

    ★ 用 **tree 接口一次问完**，不是逐个发 HEAD。逐个 HEAD 踩过：并发一大就被
      镜像限流，实测 8 并发时同一个仓库的 12 个包**全部**失败（size 全 -1，
      白等 24 秒），而这个仓库单独发一次 HEAD 明明是好的一（2 秒 200，63MB）。
      tree 接口一次请求、1 秒，把整个仓库的文件大小都带回来（实测合计 830 MB）。
      真拿不到才回落到逐个 HEAD，并发压到 3 并重试 —— 慢慢来，别把镜像打死。

    合集仓库动辄几十个包、几个 GB（实测小马宝莉那个合集 54 个包 / 3.5GB）。
    不知道体积，用户就没法决定"要不要全装"：他要的是**自己决定下载**，
    那就得把代价摆在明面上。问不到就留 0，不猜一个数糊弄。
    """
    by_path = {}
    try:
        # ★ 只问主源、**短超时、不换源**：这个接口正常 0.7 秒，但慢起来能拖到 45 秒
        #   （实测同一条 URL 前后两次，一次 45 秒超时、一次 0.7 秒）。
        #   为了"显示个体积"把用户摁在那儿等 45 秒不值得 —— 拿不到就回落逐个 HEAD。
        rows = json.loads(_get(HOSTS[0] + "/api/models/%s/tree/main?recursive=1"
                               % urllib.parse.quote(repo, safe="/"), timeout=6))
        for x in rows or []:
            if x.get("type") == "file" and x.get("path"):
                sz = int(x.get("size") or 0)
                by_path[x["path"]] = sz
                by_path.setdefault(os.path.basename(x["path"]), sz)
    except Exception:
        by_path = {}

    def one(p):
        sz = by_path.get(p["file"])
        if sz is None:
            sz = by_path.get(os.path.basename(p["file"]))
        if sz is None:
            for i in range(2):
                sz = max(0, file_size(file_url(repo, p["file"])))
                if sz:
                    break
                time.sleep(0.6 * (i + 1))
        p["size"] = sz or 0
        return p

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, packs[:max_n]))


def detail(repo, sizes=False):
    """安装前读一次仓库。**这里不套"像不像音色模型"的过滤**：用户是自己挑中
    这个仓库的，过滤器的职责是"别往搜索结果里塞垃圾"，不是"替用户决定他选的
    东西不算"。踩过：Loren85/Alastor 明明在、zip 也在，却被判成"读不到这个仓库"。

    报错也要分清楚 —— 到底是**读不到**（网络/仓库不存在）还是**里面没权重**，
    这两种的下一步完全不同（前者重试，后者换一个仓库），写成一句话就是在骗人。
    """
    # ★★ "没读到"和"里面没权重"必须分清，而且网络抖一下值得多试一次：
    #   这是用户**自己挑中**的仓库，读不到就等于装不了。
    #   踩过：一次网络抖动被报成"这个仓库里没有能装的权重文件"，用户以为分享者
    #   发的是空包、换一个仓库去装 —— 其实那一次请求压根没成功，再点一次就好了。
    stats = {"net": 0, "ok": 0}
    d = None
    for _ in range(2):
        d = _detail(repo, strict=False, stats=stats)
        if d or not stats.get("net"):
            break
        time.sleep(0.8)
    if d:
        if sizes and d.get("packs"):
            _fill_sizes(repo, d["packs"])
            d["total_mb"] = round(sum(p.get("size") or 0
                                      for p in d["packs"]) / 1048576.0)
        return d
    # 走到这儿说明重试过了还是没有 —— 先把"那一次没读到"如实说出来，别赖仓库
    if stats.get("net"):
        return {"error": "读不到这个仓库（网络不通，已经重试过一次），稍后再点一次"}
    try:
        api("/api/models/" + urllib.parse.quote(repo, safe="/"))
    except _NoRepo:
        return {"error": "这个仓库不存在，或者作者没公开（HTTP 401/404）"}
    except Exception as e:
        return {"error": "读不到这个仓库（%s）" % str(e)[:80]}
    return {"error": "这个仓库里没有能装的权重文件（要 .pth，或装着 .pth 的 .zip）"}


def file_url(repo, fname, host=None):
    h = host or HOSTS[0]
    return "%s/%s/resolve/main/%s" % (h, repo, urllib.parse.quote(fname, safe=""))


def file_size(url):
    """问一个文件多大（HEAD）。失败返回 -1（不抛：下载前先问大小只是为了显示）。"""
    try:
        req = urllib.request.Request(url, headers=UA_HEADERS, method="HEAD")
        r = urllib.request.urlopen(req, timeout=TIMEOUT)
        return int(r.headers.get("Content-Length") or -1)
    except Exception:
        return -1


def download_dir(repo):
    d = os.path.join(DOWNLOADS, re.sub(r"[^0-9A-Za-z_\-\.]+", "_", repo))
    os.makedirs(d, exist_ok=True)
    return d


def pick_weight(files):
    """从仓库的文件清单里挑该下哪一个。

    顺序：压缩包（通常 pth+index 齐全）> .pth（RVC 权重本体）> .ckpt。
    索引 .index 另行下载（它常常跟权重同名不同后缀）。
    """
    low = [(f, f.lower()) for f in files]
    for f, l in low:
        if l.endswith(PACK_EXT):
            return f, "pack"
    for f, l in low:
        if l.endswith(".pth") and "index" not in l:
            return f, "rvc"
    for f, l in low:
        if l.endswith(".ckpt"):
            return f, "sovits"
    for f, l in low:
        if l.endswith(".safetensors"):
            return f, "safetensors"
    return None, None


def list_packs(files):
    """这个仓库里一共有哪些「可装的包」。

    ★★ 为什么要单列这个：小马宝莉、地狱客栈这类作品，HF 上大多是**合集仓库** ——
    实测 MTNProductions/MyLittlePony_RVC2 一个仓库里躺着 14 个角色的 zip
    （Applejack / Fluttershy / Pinkie Pie / Rarity / Twilight Sparkle…），
    MusicBox27/MyLittlePonyRVC 里也有十几个。只按"挑第一个文件"装，用户点一次
    「安装」拿到的就是文件清单最靠前的那个边角角色，而他真正要的是**全角色**。
    所以安装必须能一次把整个合集装完。
    """
    out = []
    for f in files or []:
        low = f.lower()
        if low.startswith(".") or low.endswith((".md", ".index", ".txt", ".json")):
            continue
        if low.endswith(PACK_EXT) or low.endswith((".pth", ".ckpt")):
            nm = clean_name(f)
            out.append({"file": f, "name": nm, "cn": to_cn(nm)})
    return out


def clean_name(name):
    """把 'Alastor-Hazbin-hotel-ita.zip' / 'Twilight Sparkle (Español Latino) - Weights Model.zip'
    洗成一个能看的角色名。

    ★ 为什么取第一段：分享者习惯把「角色-作品-语言-版本」全塞进文件名
    （Alastor-Hazbin-hotel-ita、Charlie-Hazbin-Hotel-Ita-V2、Emily-Ita-300）。
    不切的话角色卡上会出现一长串谁都不认识的名字；切开之后剩下的那一段
    通常就是角色本身。切完再洗掉括号里的语种/版本信息。
    """
    n = os.path.splitext(os.path.basename(name))[0]
    # 先按分隔符切开，取**第一个像人名的段**（长度 ≥2 且不是纯数字/语种标记）
    parts = [p.strip() for p in re.split(r"\s+-\s+|[-_]", n) if p.strip()]
    keep = None
    for p in parts:
        if len(p) >= 2 and not re.fullmatch(r"(?i)(v\d+|ita|ita\d*|italiano?|eng|english|"
                                            r"espa[nñ]ol|latino?|latin|spanish|\d+e|\d+|"
                                            r"weights?|model|rvc.*|hazbin.*|hotel|mlp|"
                                            r"equestria.*|voice.*|sing.*)", p):
            keep = p
            break
    n = keep or (parts[0] if parts else n)
    n = re.sub(r"\((?:espa[nñ]ol\s*latino|latin\s*spanish|italiano?|ita|english|eng|.*dub.*)\)",
               "", n, flags=re.I)
    n = re.sub(r"\[[^\]]*\]", "", n)
    n = re.sub(r"\s+", " ", n).strip(" -_")
    return n[:40] or "未命名角色"
