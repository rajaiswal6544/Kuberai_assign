"""Deterministic replies. Every rupee/gram figure is inserted by the backend, never the LLM.

Two registers: "en" and "hinglish" (Devanagari input also gets the Hinglish text)."""

from app.money import format_inr

_T: dict[str, dict[str, str]] = {
    "greeting": {
        "en": "Hi! I can show you today's gold price, buy digital gold for you (from {min}), or set up a daily, weekly or monthly gold SIP.",
        "hinglish": "Namaste! Main aapko aaj ka gold price bata sakta hoon, digital gold khareed sakta hoon ({min} se shuru), ya daily, weekly ya monthly gold SIP set kar sakta hoon.",
    },
    "ask_amount": {
        "en": "How much would you like to put into gold? Anything from {min} to {max}.",
        "hinglish": "Kitne ka gold lena hai? {min} se {max} tak kuch bhi.",
    },
    "price_only": {
        "en": "Gold is {price} per gram right now, plus 3% GST. Tell me an amount and I'll show exactly how much gold it buys.",
        "hinglish": "Abhi gold {price} per gram hai, plus 3% GST. Amount bataiye, main exact gold quantity dikhata hoon.",
    },
    "quote": {
        "en": "For {amount} at {price}/g you get {grams} g of gold.\n- Gold value: {gold_value}\n- GST (3%): {gst}\n- Total charged: {total}\n{unspent}Reply \"yes\" to buy or \"no\" to skip. This price is held for {ttl} minutes.",
        "hinglish": "{amount} mein {price}/g ke rate pe aapko {grams} g gold milega.\n- Gold value: {gold_value}\n- GST (3%): {gst}\n- Total charge: {total}\n{unspent}Khareedne ke liye \"haan\" likhiye, ya \"nahi\". Yeh price {ttl} minute ke liye hold hai.",
    },
    "unspent": {
        "en": "- Not charged: {unspent} (gold is bought in 0.0001 g steps, so these paise can't buy another step)\n",
        "hinglish": "- Charge nahi hoga: {unspent} (gold 0.0001 g ke steps mein milta hai, isliye yeh paise next step ke liye kam hain)\n",
    },
    "quote_refreshed": {
        "en": "That price expired, so nothing was charged. Here's a fresh quote:\n",
        "hinglish": "Woh price expire ho gaya, kuch charge nahi hua. Yeh naya quote hai:\n",
    },
    "purchase_done": {
        "en": "Done! You bought {grams} g of gold at {price}/g. Total charged: {total}. It's in your holdings now.",
        "hinglish": "Ho gaya! Aapne {price}/g pe {grams} g gold khareeda. Total charge: {total}. Yeh aapki holdings mein add ho gaya hai.",
    },
    "purchase_replayed": {
        "en": "That purchase is already done: {grams} g for {total}. You were not charged again.",
        "hinglish": "Yeh purchase pehle hi ho chuki hai: {grams} g, {total} mein. Dobara charge nahi hua.",
    },
    "nothing_pending": {
        "en": "There's nothing waiting for your confirmation right now. Tell me an amount to buy, or ask for a SIP.",
        "hinglish": "Abhi confirm karne ke liye kuch pending nahi hai. Amount bataiye ya SIP ke liye kahiye.",
    },
    "declined": {
        "en": "No problem, nothing was bought. I'm here whenever you want to check the price or start small.",
        "hinglish": "Koi baat nahi, kuch nahi khareeda gaya. Jab chahein price dekh sakte hain ya chhote amount se shuru kar sakte hain.",
    },
    "ambiguous": {
        "en": "Happy to help you invest{amount_part}. Do you want to buy digital gold once, or set up a SIP that buys gold regularly?",
        "hinglish": "Zaroor{amount_part}. Kya aap ek baar digital gold lena chahenge, ya SIP set karna chahenge jo regular gold khareedta rahe?",
    },
    "advice_unavailable": {
        "en": "Advice is temporarily unavailable, but I can still show you the current gold price or set up a purchase.",
        "hinglish": "Advice abhi temporarily available nahi hai, lekin main gold price dikha sakta hoon ya purchase set up kar sakta hoon.",
    },
    "price_unavailable": {
        "en": "Gold prices are temporarily unavailable, so nothing was charged. Please try again in a few minutes.",
        "hinglish": "Gold price abhi temporarily available nahi hai, isliye kuch charge nahi hua. Thodi der baad try kijiye.",
    },
    "out_of_scope": {
        "en": "Sorry, that's outside what I can help with here. I can show you the gold price, buy digital gold for you, set up or manage a gold SIP, or show your holdings.",
        "hinglish": "Maaf kijiye, yeh main yahan help nahi kar sakta. Main gold price dikha sakta hoon, digital gold khareed sakta hoon, gold SIP set ya manage kar sakta hoon, ya aapki holdings dikha sakta hoon.",
    },
    "injection": {
        "en": "I can't do that. I can only help with gold prices, buying digital gold at the current price, and gold SIPs, and every purchase needs your confirmation.",
        "hinglish": "Yeh main nahi kar sakta. Main sirf gold price, current price pe digital gold khareedne, aur gold SIP mein help kar sakta hoon, aur har purchase aapke confirm karne pe hi hota hai.",
    },
    "unknown": {
        "en": "I didn't quite get that. I can show the gold price, buy digital gold (from {min}), or set up a gold SIP.",
        "hinglish": "Samajh nahi aaya. Main gold price dikha sakta hoon, digital gold khareed sakta hoon ({min} se), ya gold SIP set kar sakta hoon.",
    },
    "sip_need_frequency": {
        "en": "How often should the SIP of {amount} buy gold: daily, weekly or monthly?",
        "hinglish": "{amount} ka SIP kitni baar gold khareede: daily, weekly ya monthly?",
    },
    "sip_need_amount": {
        "en": "How much should each SIP instalment be? Anything from {min} to {max}.",
        "hinglish": "Har SIP instalment kitne ka ho? {min} se {max} tak.",
    },
    "sip_proposal": {
        "en": "Here's your SIP: {schedule}, first instalment on {first}.{notes} Each instalment buys gold at that day's price, GST included. Reply \"yes\" to start it.",
        "hinglish": "Aapka SIP: {schedule}, pehli instalment {first} ko.{notes} Har instalment us din ke price pe gold khareedegi, GST included. Shuru karne ke liye \"haan\" likhiye.",
    },
    "sip_exists": {
        "en": "You already have this SIP running: {schedule}, next instalment on {next}. I didn't create another one.",
        "hinglish": "Yeh SIP pehle se chal raha hai: {schedule}, agli instalment {next} ko. Maine naya nahi banaya.",
    },
    "sip_confirmed": {
        "en": "Your SIP is active: {schedule}. First instalment on {next}.",
        "hinglish": "Aapka SIP active hai: {schedule}. Pehli instalment {next} ko.",
    },
    "proposal_expired": {
        "en": "That SIP proposal timed out, so nothing was set up. Ask again whenever you like.",
        "hinglish": "Woh SIP proposal time out ho gaya, kuch set up nahi hua. Jab chahein dobara kahiye.",
    },
    "sip_none": {
        "en": "You don't have a SIP to {action}.",
        "hinglish": "Aapka koi SIP nahi hai jise {action} kiya ja sake.",
    },
    "sip_which": {
        "en": "You have more than one SIP you could {action}. Which one?\n{options}",
        "hinglish": "Aapke ek se zyada SIP hain jinhe {action} kiya ja sakta hai. Kaunsa?\n{options}",
    },
    "sip_paused": {
        "en": "Paused your SIP ({schedule}). No instalments will run until you resume it.",
        "hinglish": "Aapka SIP ({schedule}) pause kar diya. Resume karne tak koi instalment nahi chalegi.",
    },
    "sip_resumed": {
        "en": "Resumed your SIP ({schedule}). Next instalment on {next}; paused dates are not backfilled.",
        "hinglish": "Aapka SIP ({schedule}) resume ho gaya. Agli instalment {next} ko; pause wale dino ki instalment nahi kategi.",
    },
    "sip_cancelled": {
        "en": "Cancelled your SIP ({schedule}). Gold already bought stays in your holdings.",
        "hinglish": "Aapka SIP ({schedule}) cancel kar diya. Pehle khareeda gold aapki holdings mein rahega.",
    },
    "sip_list_empty": {
        "en": "You don't have any SIPs yet. Want to start one from {min}?",
        "hinglish": "Abhi aapka koi SIP nahi hai. {min} se shuru karein?",
    },
    "sip_list": {
        "en": "Your SIPs:\n{lines}",
        "hinglish": "Aapke SIPs:\n{lines}",
    },
    "holdings": {
        "en": "You hold {grams} g of gold from {count} purchase(s), {total} paid in total.",
        "hinglish": "Aapke paas {grams} g gold hai, {count} purchase(s) se, total {total} diya.",
    },
    "holdings_empty": {
        "en": "You don't hold any gold yet. You can start with as little as {min}.",
        "hinglish": "Abhi aapke paas gold nahi hai. Sirf {min} se shuru kar sakte hain.",
    },
    "empty_message": {
        "en": "Type a message, e.g. 'Buy gold worth 500' or 'What is the gold price?'",
        "hinglish": "Kuch likhiye, jaise 'Buy gold worth 500' ya 'gold ka price kya hai?'",
    },
    "digits_please": {
        "en": "Please type the amount in digits, like 500 or 2,000.",
        "hinglish": "Amount digits mein likhiye, jaise 500 ya 2,000.",
    },
    "which_amount": {
        "en": "I see more than one amount ({amounts}). Which one should I use?",
        "hinglish": "Aapke message mein ek se zyada amount hain ({amounts}). Kaunsa use karun?",
    },
    "grams_quote": {
        "en": "Purchases are made by rupee amount. At {price}/g, the smallest amount that buys at least {grams} g is {amount} (gold value {gold_value} + GST {gst}). Want me to quote {amount}?",
        "hinglish": "Purchase rupee amount se hota hai. {price}/g pe kam se kam {grams} g ke liye {amount} chahiye (gold value {gold_value} + GST {gst}). {amount} ka quote banau?",
    },
    "grams_too_much": {
        "en": "{grams} g would cost about {amount}, which is above the {max} limit per purchase. Try a smaller quantity or a rupee amount.",
        "hinglish": "{grams} g lagbhag {amount} ka hoga, jo {max} ki limit se zyada hai. Kam quantity ya rupee amount try kijiye.",
    },
    "sell_unavailable": {
        "en": "Selling gold isn't available in this assistant yet. {holdings}",
        "hinglish": "Is assistant mein abhi gold bechna available nahi hai. {holdings}",
    },
    "sip_cancel_confirm": {
        "en": "Cancelling your SIP ({schedule}) is permanent. Reply \"yes\" to cancel it, or \"no\" to keep it.",
        "hinglish": "SIP ({schedule}) cancel karna permanent hai. Cancel karne ke liye \"haan\", rakhne ke liye \"nahi\" likhiye.",
    },
    "sip_kept": {
        "en": "Okay, your SIP ({schedule}) stays as it is.",
        "hinglish": "Theek hai, aapka SIP ({schedule}) waise hi rahega.",
    },
    "cancel_request_expired": {
        "en": "That cancel request timed out, so your SIP was not cancelled. Ask again if you still want to cancel it.",
        "hinglish": "Woh cancel request time out ho gayi, SIP cancel nahi hua. Agar abhi bhi cancel karna hai to dobara kahiye.",
    },
    "sip_proposal_waiting": {
        "en": "This SIP is already waiting for your confirmation: {schedule}, first instalment on {first}. Reply \"yes\" to start it.",
        "hinglish": "Yeh SIP pehle se aapke confirmation ka wait kar raha hai: {schedule}, pehli instalment {first} ko. Shuru karne ke liye \"haan\" likhiye.",
    },
    "advice_fallback": {
        "en": "{price_line}Gold prices move with interest rates, inflation, the rupee and global demand, and can fall as well as rise. Detailed advice is temporarily unavailable, but I can show the price or set up a purchase.",
        "hinglish": "{price_line}Gold ka price interest rates, inflation, rupee aur global demand se badalta hai, aur gir bhi sakta hai. Detailed advice abhi temporarily available nahi hai, lekin main price dikha sakta hoon ya purchase set up kar sakta hoon.",
    },
    "compare_fallback": {
        "en": "In short: an FD pays a fixed, known return and protects your capital; gold pays no interest, its price can rise or fall, and it is easy to sell. The two are also taxed differently. A more detailed comparison is temporarily unavailable.",
        "hinglish": "Short mein: FD ek fixed, pehle se pata return deta hai aur capital safe rakhta hai; gold pe interest nahi milta, uska price upar-neeche hota hai, aur use bechna aasaan hai. Dono pe tax bhi alag lagta hai. Detailed comparison abhi temporarily available nahi hai.",
    },
    "price_line": {
        "en": "Gold is {price} per gram right now, plus 3% GST. ",
        "hinglish": "Abhi gold {price} per gram hai, plus 3% GST. ",
    },
}


def render(key: str, language: str, **kw: object) -> str:
    variants = _T[key]
    lang = "en" if language == "en" else "hinglish"
    return variants[lang].format(**kw)


def inr(value) -> str:
    return format_inr(value)
