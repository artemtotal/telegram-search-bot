"""07.11.2026 (Артём): правила чату «Потсдам» → telegra.ph (сторінка, яку можна потім правити тим самим токеном) і короткий
виклад + питання за правилами → /app/config/join_gate.json (перевірка нових учасників). Запуск у контейнері tgbot:
    python /app/tools/publish_rules_2026_11_07.py
Токен Telegraph і шлях сторінки зберігаються в /app/config/telegraph.json — повторний запуск редагує ту саму сторінку."""
import json
import os
import urllib.parse
import urllib.request

TG = "/app/config/telegraph.json"
CFG = "/app/config/join_gate.json"
GROUP = "https://t.me/UkrainischesBrandenburg"
GERMAN_TOPIC = f"{GROUP}/183965"


def api(method, **params):
    data = urllib.parse.urlencode({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in params.items()}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("https://api.telegra.ph/" + method, data=data), timeout=30))
    if not r.get("ok"):
        raise SystemExit(f"telegraph {method}: {r}")
    return r["result"]


def p(*parts):
    return {"tag": "p", "children": list(parts)}


def b(t):
    return {"tag": "b", "children": [t]}


def a(t, href):
    return {"tag": "a", "attrs": {"href": href}, "children": [t]}


def h(t):
    return {"tag": "h4", "children": [t]}


def ul(*items):
    return {"tag": "ul", "children": [{"tag": "li", "children": list(i) if isinstance(i, tuple) else [i]} for i in items]}


CONTENT = [
    p("Чат для українців у Потсдамі, Бранденбурзькій області та Берліні. Будь ласка, прочитайте правила — вони коротші, ніж здається 🙂"),
    h("🤖 Корисне в чаті"),
    ul(("Потбот прямо в чаті: напишіть ", b("«Потбот» і пару слів"), " (наприклад: Потбот житло, Потбот перевізник) — бот пошукає по всій історії чату й дасть розгорнуту відповідь."),
       ("В особистих повідомленнях ", a("@Potsdambot", "https://t.me/Potsdambot"), " стежить за новими квартирами на порталах Потсдама й одразу надсилає вам, повідомляє про вільні терміни (посольство, ДП «Документ») і відповідає на питання."),
       ("🇩🇪 Учимо німецьку: щодня урок і вікторина в ", a("болталці", GERMAN_TOPIC), "."),
       ("🎭 Афіша подій Потсдама й Берліна від ", a("@novini_potsdam_bot", "https://t.me/novini_potsdam_bot"), " — у четвер увечері на вихідні та в неділю зранку."),
       ("📢 Реклама в чаті — через ", a("@AdvertisingPotsdam_bot", "https://t.me/AdvertisingPotsdam_bot"), " або ", a("@artemtotal", "https://t.me/artemtotal"), " (див. п. 1).")),
    h("1. Реклама 📢"),
    ul("1.1 Розміщення реклами не частіше ніж 1 раз на 3 дні.",
       "1.2 З 1 червня безкоштовна реклама в нашому чаті більше не доступна. Вартість одного поста — 3 євро. При покупці комплексного пакета від 10 постів вартість одного поста — 2,5 євро.",
       "1.3 Кошти направляються на оплату роботи бота по боротьбі зі спамом, очистку групи та підтримання реальної активності. Всі надлишки коштів передаються на підтримку ЗСУ.",
       "1.4 Реклама від некомерційних, благодійних та волонтерських організацій розміщується безкоштовно! 🙌",
       ("1.5 Щодо розміщення реклами звертайтесь до ", a("@artemtotal", "https://t.me/artemtotal"), " або через бота ", a("@AdvertisingPotsdam_bot", "https://t.me/AdvertisingPotsdam_bot"), ". Реклама, яка не була погоджена з адмінами чату, видаляється."),
       "1.6 Свої послуги дозволено рекламувати не частіше 1 разу на три дні. Оголошення старше 1 тижня видаляються."),
    h("2. Атмосфера в чаті 🤗"),
    ul("2.1 Потрібно дружити і поважати один одного.", "2.2 Мат і вказівки, що робити, без прохання про допомогу заборонені. 🚫"),
    h("3. Повідомлення 💬"),
    ul("3.1 Пишіть своє питання у відповідній темі.", "3.2 Дублювати в кілька тем не можна."),
    h("4. Теми 🗂️"),
    ul("4.1 Тема «Загальні питання» — для питань, які не підпадають під інші теми.",
       "4.2 Тема «Болталка» — просто для спілкування і всякого роду спаму. Тут не модеруємо спам. Тут же щодня — урок німецької 🇩🇪."),
    h("5. Бот 🤖"),
    ul("5.1 Ваше повідомлення має починатися зі слова «Потбот», далі коротко — про що потрібна інформація (пару слів достатньо). Наприклад: Потбот житло, Потбот перевізник, Потбот перекладач.",
       "5.2 Бот видасть розгорнуту відповідь. Якщо ні — спробуйте спитати іншими словами або такої інформації ще немає. Тоді скористайтеся пошуком слів чи запитайте в групі.",
       "5.3 Бот знає українську, російську і трохи німецьку."),
    h("6. Пошук слів 🔍"),
    ul("6.1 Перед тим, як задавати питання, скористайтеся пошуком слів у чаті: 90% тем уже обговорювались, у групі є відповіді, відгуки та поради.",
       "6.2 Якщо відповіді не знайшлося — пишіть питання у відповідну тему, там вам якнайшвидше допоможуть."),
    h("7. Інформація про місто 🌆"),
    p("7.1 Вся інформація про наше місто: ", a("uahelp.wiki/potsdam", "https://uahelp.wiki/potsdam"), "."),
    h("8. Заснування групи 🎗️"),
    ul("8.1 Групу засновано з метою інформаційної підтримки мешканців України, які вимушено опинились у Німеччині внаслідок російської агресії.",
       "8.2 У групі вітаються українці, громадяни інших країн, які постійно проживали в Україні, та особи, які бажають їм допомогти.",
       "8.3 Ваше перебування в групі означає: солідарність з Україною та беззаперечну підтримку в її боротьбі проти російської агресії 🇺🇦; беззаперечне засудження російської агресії; зобов'язання беззаперечно дотримуватися законів Німеччини у своїх комунікаціях у групі, зокрема антидискримінаційних 🇩🇪.",
       "8.4 Якщо ви не погоджуєтесь із вищенаведеним, ви можете покинути групу.",
       "8.5 Адміністрація групи вітає критичну думку, проте не толерує антиукраїнські висловлювання.",
       "8.6 Висловлюючи критику, пам'ятайте, що серед учасників групи є сім'ї військовослужбовців і військовослужбовиць, а також багато людей, які втратили своїх коханих і домівки. Будьте чутливими та поважними до цих травм і втрат, спричинених російською агресією. ❤️"),
    h("9. Система попереджень ⚠️"),
    ul("9.1 Час попередження — 4 тижні. Старіші попередження видаляються автоматично.",
       "9.2 Ліміт попереджень у чаті: 2. Коли ліміт перевищено, користувача блокують.",
       "9.3 2 попередження протягом місяця від адмінів — і з третього користувач автоматично блокується без можливості писати в чат."),
    h("10. Зв'язок з адміністрацією 📬"),
    ul("10.1 Щоб покликати адмінів, відповідайте на спам з тегом @admin.",
       ("10.2 Пропозиції щодо роботи чату надсилайте в особисті ", a("@artemtotal", "https://t.me/artemtotal"), "."),
       ("10.3 Питання щодо роботи чату (чому мене забанили, чому не можу писати тощо) вирішуються в будні з 17 до 18 в особистих повідомленнях ", a("@artemtotal", "https://t.me/artemtotal"), ".")),
]

st = json.load(open(TG, encoding="utf-8")) if os.path.exists(TG) else {}
if not st.get("token"):
    acc = api("createAccount", short_name="PotsdamChat", author_name="Чат «Потсдам»", author_url=GROUP)
    st = {"token": acc["access_token"]}
if st.get("path"):
    page = api("editPage", access_token=st["token"], path=st["path"], title="Правила чату «Потсдам»", author_name="Чат «Потсдам»",
               author_url=GROUP, content=CONTENT, return_content="false")
else:
    page = api("createPage", access_token=st["token"], title="Правила чату «Потсдам»", author_name="Чат «Потсдам»",
               author_url=GROUP, content=CONTENT, return_content="false")
st.update(path=page["path"], url=page["url"])
json.dump(st, open(TG, "w", encoding="utf-8"), ensure_ascii=False)
os.chmod(TG, 0o600)
URL = page["url"]
print("RULES:", URL)

# ---------- перевірка нових учасників: короткий виклад правил + питання за правилами ----------
cfg = json.load(open(CFG, encoding="utf-8"))
cfg["texts"]["uk"]["rules"] = (
    "📌 <b>Коротко про правила</b>\n• Реклама — лише погоджена з адміном (@AdvertisingPotsdam_bot), не частіше 1 разу на 3 дні.\n"
    "• Поважайте одне одного: без мату й непрошених вказівок.\n• Питання — у відповідну тему, не дублюйте в кількох.\n"
    "• Перебування в групі означає солідарність з Україною 🇺🇦 і дотримання законів Німеччини 🇩🇪.\n"
    "• Потрібна інформація — напишіть у чаті «Потбот» і пару слів: бот пошукає по всій історії чату.\n\n"
    f"📖 <a href=\"{URL}\">Повні правила чату</a>")
cfg["texts"]["ru"]["rules"] = (
    "📌 <b>Коротко о правилах</b>\n• Реклама — только согласованная с админом (@AdvertisingPotsdam_bot), не чаще 1 раза в 3 дня.\n"
    "• Уважайте друг друга: без мата и непрошеных указаний.\n• Вопрос — в подходящую тему, не дублируйте в нескольких.\n"
    "• Нахождение в группе означает солидарность с Украиной 🇺🇦 и соблюдение законов Германии 🇩🇪.\n"
    "• Нужна информация — напишите в чате «Потбот» и пару слов: бот поищет по всей истории чата.\n\n"
    f"📖 <a href=\"{URL}\">Полные правила чата</a> (на украинском)")
cfg["texts"]["de"]["rules"] = (
    "📌 <b>Die Regeln kurz</b>\n• Werbung nur nach Absprache mit dem Admin (@AdvertisingPotsdam_bot), höchstens 1× in 3 Tagen.\n"
    "• Respektvoller Umgang: keine Schimpfwörter, keine ungebetenen Anweisungen.\n• Fragen ins passende Thema, nicht mehrfach posten.\n"
    "• Mitgliedschaft bedeutet Solidarität mit der Ukraine 🇺🇦 und Einhaltung der deutschen Gesetze 🇩🇪.\n"
    "• Infos gesucht? Schreiben Sie im Chat „Потбот“ und ein paar Stichworte — der Bot durchsucht den ganzen Chatverlauf.\n\n"
    f"📖 <a href=\"{URL}\">Vollständige Regeln</a> (auf Ukrainisch)")
extra = {
    "uk": f"\n🇩🇪 Щодня урок німецької — в <a href=\"{GERMAN_TOPIC}\">болталці</a>.\n🎭 Афіша подій на вихідні — у четвер увечері.\n"
          "📢 Реклама в чаті — @AdvertisingPotsdam_bot.",
    "ru": f"\n🇩🇪 Каждый день урок немецкого — в <a href=\"{GERMAN_TOPIC}\">болталке</a>.\n🎭 Афиша событий на выходные — в четверг вечером.\n"
          "📢 Реклама в чате — @AdvertisingPotsdam_bot.",
    "de": f"\n🇩🇪 Täglich eine Deutschlektion — im <a href=\"{GERMAN_TOPIC}\">Plauder-Thema</a>.\n🎭 Veranstaltungen fürs Wochenende — donnerstags abends.\n"
          "📢 Werbung im Chat — @AdvertisingPotsdam_bot.",
}
for lg, add in extra.items():
    w = cfg["texts"][lg]["welcome"]
    if "AdvertisingPotsdam_bot" not in w:
        cfg["texts"][lg]["welcome"] = w + "\n" + add
cfg["questions"] = [
    {"q": {"uk": "Як часто можна рекламувати свої послуги в чаті?", "ru": "Как часто можно рекламировать свои услуги в чате?",
           "de": "Wie oft darf man im Chat für eigene Dienstleistungen werben?"},
     "options": {"uk": ["Не частіше 1 разу на 3 дні, погодивши з адміном", "Щодня, скільки завгодно"],
                 "ru": ["Не чаще 1 раза в 3 дня, согласовав с админом", "Каждый день, сколько угодно"],
                 "de": ["Höchstens 1× in 3 Tagen, nach Absprache mit dem Admin", "Jeden Tag, so oft man will"]}, "correct": 0},
    {"q": {"uk": "Як швидко знайти відповідь в історії чату?", "ru": "Как быстро найти ответ в истории чата?",
           "de": "Wie findet man schnell eine Antwort im Chatverlauf?"},
     "options": {"uk": ["Написати «Потбот» і пару слів", "Продублювати питання в усіх темах"],
                 "ru": ["Написать «Потбот» и пару слов", "Продублировать вопрос во всех темах"],
                 "de": ["„Потбот“ und ein paar Stichworte schreiben", "Die Frage in alle Themen kopieren"]}, "correct": 0},
    {"q": {"uk": "Що означає перебування в цій групі?", "ru": "Что означает нахождение в этой группе?",
           "de": "Was bedeutet die Mitgliedschaft in dieser Gruppe?"},
     "options": {"uk": ["Солідарність з Україною й дотримання законів Німеччини", "Можна писати що завгодно"],
                 "ru": ["Солидарность с Украиной и соблюдение законов Германии", "Можно писать что угодно"],
                 "de": ["Solidarität mit der Ukraine und Einhaltung deutscher Gesetze", "Man darf alles schreiben"]}, "correct": 0},
]
json.dump(cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("join_gate: правила + 3 питання оновлено, enabled =", cfg.get("enabled"))
