from telegram.ext import Updater, CommandHandler, MessageHandler

TOKEN = '9238830575:AAFsnL8_Lf1tlar3ED7EGooirNZiWBh1JSg'

def start(update, context):
 context.bot.send_message(chat_id=update.effective_chat.id, text='Привет! Я простой бот.')

def echo(update, context):
    context.bot.send_message(chat_id=update.effective_chat.id, text=update.message.text)

def main():
    updater = Updater(token=TOKEN, use_context=True)

    dp = updater.dispatcher

    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, echo))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
