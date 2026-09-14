import os
import base64
from pathlib import Path

# These modules help us read and create emails.
from email import policy
from email.parser import BytesParser
from email.message import EmailMessage

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv
from openai import OpenAI


# Load the private API key from the .env file beside this script, even when
# the program is started from a different folder.
env_file = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_file)


# The program needs permission to read, change, and send Gmail messages.
GMAIL_PERMISSIONS = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def connect_to_gmail():
    """
    Authenticate with Gmail and return the Gmail API service.
    """

    # Start without saved login details.
    credentials = None

    if os.path.exists("token.json"):
        # Reuse the saved login if it is already on this computer.
        credentials = Credentials.from_authorized_user_file(
            "token.json",
            GMAIL_PERMISSIONS
        )

    if not credentials or not credentials.valid:

        if credentials and credentials.expired and credentials.refresh_token:
            # Refresh the login instead of asking the user to log in again.
            credentials.refresh(Request())

        else:
            # Ask the user to give the program permission to use Gmail.
            login_flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json",
                GMAIL_PERMISSIONS
            )

            credentials = login_flow.run_local_server(port=0)

        # Save the login so it can be reused next time.
        with open("token.json", "w") as token_file:
            token_file.write(credentials.to_json())

    return build("gmail", "v1", credentials=credentials)

def get_email_body(email_message):
    """
    Extract the readable text body from an email.
    """

    plain_messages = []
    html_messages = []

    if email_message.is_multipart():

        # An email can have text, HTML, and attachment parts.
        for email_part in email_message.walk():

            # Ignore attachments
            if email_part.get_content_disposition() == "attachment":
                continue

            content_type = email_part.get_content_type()

            try:
                content = email_part.get_content()
            except Exception:
                continue

            if content_type == "text/plain":
                plain_messages.append(str(content))

            elif content_type == "text/html":
                html_messages.append(str(content))

    else:

        try:
            content = email_message.get_content()
        except Exception:
            content = ""

        if email_message.get_content_type() == "text/plain":
            plain_messages.append(str(content))

        elif email_message.get_content_type() == "text/html":
            html_messages.append(str(content))

    # Plain text is easier to read, so use it when it is available.
    if plain_messages:
        return "\n".join(plain_messages).strip()

    if html_messages:
        return "\n".join(html_messages).strip()

    return ""
def get_unread_messages(gmail_service):
    """
    Get every unread email currently in the inbox.
    """

    unread_messages = []
    next_page_token = None

    # Gmail may split a large result into several pages.
    while True:

        response = (
            gmail_service.users()
            .messages()
            .list(
                userId="me",
                q="is:unread in:inbox",
                maxResults=500,
                pageToken=next_page_token,
            )
            .execute()
        )

        # Add the messages from this page to the full list.
        unread_messages.extend(response.get("messages", []))

        next_page_token = response.get("nextPageToken")

        if not next_page_token:
            break

    return unread_messages


from email.utils import parseaddr


def get_email(gmail_service, email_id):

    response = (
        gmail_service.users()
        .messages()
        .get(
            userId="me",
            id=email_id,
            format="raw"
        )
        .execute()
    )

    # Gmail sends the email as encoded text, so decode it before reading it.
    decoded_email = base64.urlsafe_b64decode(
        response["raw"]
    )

    email_message = BytesParser(
        policy=policy.default
    ).parsebytes(decoded_email)

    # Convert email headers into normal strings.
    sender = str(email_message.get("From", ""))
    reply_to = str(email_message.get("Reply-To", ""))

    sender_email = parseaddr(sender)[1]
    reply_email = parseaddr(reply_to)[1]

    # Keep the useful parts of the email in one dictionary.
    return {
        "id": email_id,
        "thread_id": response.get("threadId"),

        "from": sender,
        "from_email": sender_email,

        "reply_to": reply_to,
        "reply_to_email": reply_email,

        "to": str(email_message.get("To", "")),
        "subject": str(email_message.get("Subject", "")),
        "date": str(email_message.get("Date", "")),

        "message_id": str(
            email_message.get("Message-ID", "")
        ),

        "references": str(
            email_message.get("References", "")
        ),

        "body": get_email_body(email_message),
    }

def mark_email_as_read(gmail_service, email_id):
    """
    Remove the UNREAD label from a Gmail message.
    """

    # Removing this label makes Gmail treat the email as read.
    gmail_service.users().messages().modify(
        userId="me",
        id=email_id,
        body={
            "removeLabelIds": ["UNREAD"]
        }
    ).execute()


def send_new_email(
    gmail_service,
    recipient,
    email_subject,
    email_body,
    carbon_copy=None,
    blind_carbon_copy=None,
):
    """
    Send a new email.
    """

    # Build the email before encoding and sending it through Gmail.
    email_message = EmailMessage()

    email_message["To"] = recipient
    email_message["Subject"] = email_subject

    if carbon_copy:
        email_message["Cc"] = carbon_copy

    if blind_carbon_copy:
        email_message["Bcc"] = blind_carbon_copy

    email_message.set_content(email_body)

    encoded_message = base64.urlsafe_b64encode(
        email_message.as_bytes()
    ).decode()

    # Gmail requires the whole email to be Base64 encoded.
    response = (
        gmail_service.users()
        .messages()
        .send(
            userId="me",
            body={
                "raw": encoded_message
            }
        )
        .execute()
    )

    return response

def reply_to_message(
    gmail_service,
    old_email,
    
    subject=None,
    body=""
):
    """
    Reply to an existing Gmail email inside the same thread.

    old_email should be the dictionary returned by get_email().
    """

    # Prefer the Reply-To address if the sender provided one.
    # Otherwise reply to the normal From address.
    # Reply to Reply-To when it exists, otherwise use the sender's address.
    recipient = (
        old_email.get("reply_to_email")
        or old_email.get("from_email")
    )

    if not recipient:
        raise ValueError(
            "Could not determine who to reply to."
        )

    # Use the original subject unless you provide another one.
    old_subject = old_email.get(
        "subject",
        ""
    )

    if subject is None:
        if old_subject.lower().startswith("re:"):
            subject = old_subject
        else:
            subject = f"Re: {old_subject}"

    # Create the outgoing email.
    email_message = EmailMessage()

    email_message["To"] = recipient
    email_message["Subject"] = subject

    # These headers tell email clients that this is a reply
    # to the original message.
    old_message_id = old_email.get(
        "message_id",
        ""
    )

    old_references = old_email.get(
        "references",
        ""
    )

    if old_message_id:

        email_message["In-Reply-To"] = old_message_id

        references = (
            f"{old_references} "
            f"{old_message_id}"
        ).strip()

        email_message["References"] = references

    # Add the actual reply text.
    email_message.set_content(body)

    # Gmail API expects the email encoded as Base64.
    encoded_message = base64.urlsafe_b64encode(
        email_message.as_bytes()
    ).decode()

    request_body = {
        "raw": encoded_message
    }

    # This keeps the email inside the same Gmail conversation.
    thread_id = old_email.get(
        "thread_id"
    )

    if thread_id:
        request_body["threadId"] = thread_id

    # Send the reply.
    # Send the reply through the Gmail API.
    response = (
        gmail_service.users()
        .messages()
        .send(
            userId="me",
            body=request_body
        )
        .execute()
    )

    return response

def show_email(email_data):
    """
    Put your project's email processing logic here.

    Only return successfully if the email has actually
    been processed.
    """

    print()
    print("=" * 80)
    print("FROM:", email_data["from"])
    print("SUBJECT:", email_data["subject"])
    print("DATE:", email_data["date"])
    print()
    print(email_data["body"])

    # Your project logic goes here.


def run_email_program():

    gmail_service = connect_to_gmail()

    unread_messages = get_unread_messages(gmail_service)

    print(f"Found {len(unread_messages)} unread emails.")
    # Connect to Gmail and find messages that still need processing.

    for email_item in unread_messages:

        email_id = email_item["id"]

        try:

            email_data = get_email(
                gmail_service,
                email_id
            )

            show_email(email_data)

            # Only mark as read AFTER successful processing.
            mark_email_as_read(
                gmail_service,
                email_id
            )

            print(
                f"Processed successfully: "
                f"{email_data['subject']}"
            )

        except Exception as error:

            print(
                f"Failed to process {email_id}: {error}"
            )
# Get the unread emails that will be checked by the classifier.
gmail_service = connect_to_gmail()
all_email_items = get_unread_messages(gmail_service)

for email_item in all_email_items:
    
    email_data = get_email(gmail_service, email_item["id"])
   

    # Show the email data while testing the program.
    print(email_data)
     
# Ask the AI whether the email is personal, marketing, or a reply.
classification_prompt = "Here is the data from an email:" +str(email_data) + ". Go through the email and classify it based on whether it is a " \
"personal email from another individual, a marketing email from a business/organisation, or if it is a reply to an email" \
" I previously sent. I am building a tool to identify data brokers who have my data and automate data deletion requests to each." \
" You must only output the classification as a one-word string i.e 'Personal', 'Marketing', or 'Reply'." 
api_key = os.getenv("api_key")
if not api_key:
    raise RuntimeError("Add api_key to the .env file before running this program.")

openai_client = OpenAI(api_key=api_key)

# Get the first classification from the AI.
model_response = openai_client.responses.create(
    model="gpt-4.1-mini",
    input=classification_prompt)

print(model_response.output_text)
# If it is marketing, ask the AI for the organisation's contact details.
marketing_prompt = "Here is a Marketing email." + str(email_data) + ". Output only the email address of the Data Protection Office of the organisation the" \
" email originated from, if one exists. If one does not exist, output the Customer Support or Contact email address. " \
". If none, output only the email address that the original email was sent from. Next, generate an appropriate subject for " \
"a data deletion request email to the organisation as per UK data protection laws. Then, generate the body of the email first" \
"asking whether the organisation stores any of my individual data, and if so, where they got the information from, and a deletion" \
"request." \
"email. All 3 fields must be output as a string containing only the requested information. These strings must then be" \
"stored in a dictionary with structure {'email_address': 'identified email address to send email to'" \
" 'subject': 'generated subject of the data deletion request email', 'body': 'generated body of the data deletion request email'}." 

if model_response.output_text == "Marketing":
    model_response = openai_client.responses.create(
    model="gpt-4.1-mini",
    input=marketing_prompt)
import ast
marketing_data = ast.literal_eval(model_response.output_text)

# Send a deletion request to the organisation found by the AI.
send_new_email(gmail_service, marketing_data["email_address"], marketing_data["subject"], marketing_data["body"])  
# Ask the AI whether a reply from a data broker needs more action.
reply_prompt = "Here is a reply email from a data broker." + str(email_data) + "Identify whether the reply requires further details or processing on my end" \
"or if no further action is required. If a source for where the organisation received my data is specified, output the name of " \
"that organisation too. All outputs must be in the form of a string,i.e 'COMPLETE' or 'FURTHER ACTION' for the first output and simply" \
"the name of the organisation that acted as the source for the data. The output is to be stored in the form of a dictionary" \
"with structure {'status': 'COMPLETE' or 'FURTHER ACTION', 'datasource' : 'Name of source'}"  
reply_data = email_data
if model_response.output_text == "Reply":
    model_response = openai_client.responses.create(
    model="gpt-4.1-mini",
    input=reply_prompt)

print(model_response.output_text)
from openpyxl import Workbook, load_workbook

excel_path = Path("brokers_requiring_action.xlsx")

if excel_path.exists():
    # Add to the existing spreadsheet if it has already been created.
    workbook = load_workbook(excel_path)
    worksheet = workbook.active
else:
    # Create a new spreadsheet for organisations needing action.
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Brokers"
    worksheet.append(["Organisation", "Status"])

# Parse the final AI response and save cases that need more work.
import ast
reply_result = ast.literal_eval(model_response.output_text)

if reply_result.get("status") == "FURTHER ACTION":
    organisation = (
       reply_data.get("reply_to_email") or
        reply_data.get("from_email")
        
    )

    worksheet.append([
        organisation,
        reply_result["status"],
    ])

    workbook.save(excel_path)
    

    