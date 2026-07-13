import os
os.environ["FAQ_LEARN_DAYS"] = "30"
os.environ["FAQ_LEARN_BATCH"] = "120"
os.environ["ADMIN_ID"] = "312029534"

from user_jobs.faq_learn import run_faq_learn

class FakeBot:
    def send_message(self, chat_id, text, **kw):
        print("--- MSG TO", chat_id, "---")
        print(text[:400].encode("utf-8").decode("utf-8"))
    def send_message(self, chat_id, text, reply_markup=None, **kw):
        print("--- MSG TO", chat_id, "---")
        try:
            print(text.encode("cp1252").decode("utf-8"))
        except Exception:
            print(repr(text[:200]))

class FakeContext:
    bot = FakeBot()

run_faq_learn(FakeContext())
