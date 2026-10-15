"""
One-time move of the old support_ticket documents into the Inbox
(inbox_thread + messages), keeping their ticket numbers and dates.

    python migrate_support_tickets.py

Safe to run more than once: a ticket that was already moved is skipped.
The old support_ticket documents are left untouched.
"""
from firebase_admin import firestore

import inbox_service as inbox
from database import db


def main():
    moved = skipped = 0
    for doc in db.collection('support_ticket').stream():
        t = doc.to_dict() or {}
        user_id = t.get('user_id')
        ref = db.collection(inbox.THREADS).document(f'tck_{doc.id}')
        if not user_id or ref.get().exists:
            skipped += 1
            continue

        created = t.get('created_at') or firestore.SERVER_TIMESTAMP
        replied = t.get('updated_at') or created
        has_reply = bool((t.get('admin_reply') or '').strip())
        last_text = t.get('admin_reply') if has_reply else t.get('message')
        meta = {k: t.get(k) for k in ('category', 'category_label', 'subcategory',
                                      'subcategory_label', 'reply_email') if t.get(k)}

        batch = db.batch()
        batch.set(ref, {
            'thread_no': t.get('ticket_no') or f'TCK-{doc.id[:6].upper()}',
            'type': 'report' if t.get('type') == 'problem_report' else 'support',
            'subject': (t.get('subject') or 'Support request')[:inbox.SUBJECT_MAX],
            'status': 'resolved' if t.get('status') in ('resolved', 'closed') else 'open',
            'participant_ids': [user_id, inbox.ADMIN_KEY],
            'participants': {
                user_id: {'name': t.get('user_name') or 'Family', 'role': t.get('user_role') or 'family'},
                inbox.ADMIN_KEY: {'name': inbox.ADMIN_NAME, 'role': 'admin'},
            },
            'unread': {user_id: 1 if has_reply else 0, inbox.ADMIN_KEY: 0 if has_reply else 1},
            'created_by': user_id,
            'elder_id': '', 'elder_name': '', 'related_id': '',
            'meta': meta,
            'last_message_at': replied if has_reply else created,
            'last_message_preview': inbox.preview(last_text),
            'last_sender_id': (t.get('handled_by') or '') if has_reply else user_id,
            'created_at': created,
            'updated_at': replied,
        })
        batch.set(ref.collection(inbox.MESSAGES).document(), {
            'sender_id': user_id, 'sender_key': user_id,
            'sender_name': t.get('user_name') or 'Family',
            'sender_role': t.get('user_role') or 'family',
            'body': t.get('message') or '', 'is_system': False, 'created_at': created,
        })
        if has_reply:
            batch.set(ref.collection(inbox.MESSAGES).document(), {
                'sender_id': t.get('handled_by') or '', 'sender_key': inbox.ADMIN_KEY,
                'sender_name': t.get('handled_by_name') or inbox.ADMIN_NAME,
                'sender_role': 'admin',
                'body': t['admin_reply'], 'is_system': False, 'created_at': replied,
            })
        batch.commit()
        moved += 1

    print(f'Moved {moved} ticket(s) to the inbox, skipped {skipped}.')


if __name__ == '__main__':
    main()
