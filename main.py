from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = '9238830575:AAG3cFGwbUa63NUkeLjwJsZM7xRcpq3EyKk'

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
 await context.bot.send_message(chat_id=update.effective_chat.id, text='Привет! Я простой бот.')

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text=update.message.text)

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    application.run_polling()

if __name__ == '__main__':
    main()
