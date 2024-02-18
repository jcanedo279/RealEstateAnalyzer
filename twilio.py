from twilio.rest import Client

# Your Twilio account SID and auth token from twilio.com/console
account_sid = 'your_account_sid'
auth_token = 'your_auth_token'
client = Client(account_sid, auth_token)

message = client.messages.create(
    to="+yourPhoneNumber", 
    from_="+yourTwilioNumber",
    body="Hello, I need help with my script!")

print(message.sid)