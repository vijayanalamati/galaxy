# (c) 2012-2018, Ansible by Red Hat
#
# This file is part of Ansible Galaxy
#
# Ansible Galaxy is free software: you can redistribute it and/or modify
# it under the terms of the Apache License as published by
# the Apache Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# Ansible Galaxy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# Apache License for more details.
#
# You should have received a copy of the Apache License
# along with Galaxy.  If not, see <http://www.apache.org/licenses/>.

import logging
import celery

from galaxy.main import models
from django.contrib.sites.models import Site
from django.conf import settings
from django.core import mail
from allauth.account.models import EmailAddress


LOG = logging.getLogger(__name__)


class NotificationManger(object):
    def __init__(self, email_template, preferences_name, preferences_list,
                 subject, db_message=None, repo=None):
        self.email_template = email_template
        self.preferences_name = preferences_name
        self.preferences_list = preferences_list
        self.subject = subject
        self.url = settings.GALAXY_URL.format(
            site=Site.objects.get_current().domain
        )

        self.repo = repo
        if db_message is None:
            self.db_message = subject
        else:
            self.db_message = db_message

    def render_email(self, context):
        text = self.email_template.format(**context)
        footer = email_footer_template.format(
            preferences_link='{}/me/preferences/'.format(self.url)
        )

        return text + footer

    def send(self, email_message):
        for user in self.preferences_list:

            # Create in app notification
            try:
                if user.preferences['ui_' + self.preferences_name]:
                    models.UserNotification.objects.create(
                        user=user.user,
                        type=self.preferences_name,
                        message=self.db_message,
                        repository=self.repo
                    )
            except Exception as e:
                LOG.error(e)

            # Create email notification
            try:
                if user.preferences[self.preferences_name]:
                    email = EmailAddress.objects.filter(
                        primary=True,
                        user=user.user,
                    )

                    mail.send_mail(
                        self.subject,
                        email_message,
                        settings.GALAXY_NOTIFICATION_EMAIL,
                        [email[0].email],
                        fail_silently=False
                    )
            except Exception as e:
                LOG.error(e)

    def notify(self, context):
        email = self.render_email(context)
        self.send(email)


def email_verification(email, code, username):
    url = settings.GALAXY_URL.format(
        site=Site.objects.get_current().domain
    )

    url += '/me/preferences/?verify=' + code

    message = email_verification_template.format(
        username=username,
        url=url
    )

    mail.send_mail(
        'Ansible Galaxy Please Confirm Your E-mail Address',
        message,
        settings.GALAXY_NOTIFICATION_EMAIL,
        [email],
        fail_silently=True
    )


@celery.task
def import_status(task_id, user_initiated, has_failed=False):
    task = models.ImportTask.objects.get(id=task_id)
    repo = task.repository
    owners = repo.provider_namespace.namespace.owners.all()
    author = repo.provider_namespace.namespace.name

    # Commenting this out for now because it has potential to confuse usersself
    # TODO: Add an option to enable this as a preference
    # If the import is kicked off manually, don't notify the person starting it
    # if user_initiated:
    #     user = task.owner
    #     owners = owners.exclude(pk=user.id)

    owners = _get_preferences(owners)

    if has_failed:
        preference = 'notify_import_fail'
        status = 'failed'
    else:
        preference = 'notify_import_success'
        status = 'succeeded'

    subject = 'Ansible Galaxy: import of {} has {}'.format(repo.name, status)
    db_message = 'Import {}: {}'.format(status, repo.name)

    log_path = '/my-imports'

    notification = NotificationManger(
        email_template=import_status_template,
        preferences_name=preference,
        preferences_list=owners,
        subject=subject,
        db_message=db_message,
        repo=repo
    )

    ctx = {
        'status': status,
        'content_name': '{}.{}'.format(author, repo.name),
        'import_url': notification.url + log_path
    }

    notification.notify(ctx)


@celery.task
def collection_update(repo_id):
    followers = models.UserPreferences.objects.filter(
        repositories_followed__pk=repo_id
    )

    repo = models.Repository.objects.get(id=repo_id)
    author = repo.provider_namespace.namespace.name

    notification = NotificationManger(
        email_template=update_collection_template,
        preferences_name='notify_content_release',
        preferences_list=followers,
        subject='Ansible Galaxy: New version of ' + repo.name,
        db_message='New version of: {}'.format(repo.name),
        repo=repo
    )

    path = '/{}/{}/'.format(repo.provider_namespace.namespace.name, repo.name)

    ctx = {
        'namespace_name': author,
        'content_name': repo.name,
        'content_url': notification.url + path
    }

    notification.notify(ctx)


@celery.task
def author_release(repo_id):
    repo = models.Repository.objects.get(id=repo_id)
    namespace = repo.provider_namespace.namespace
    followers = models.UserPreferences.objects.filter(
        namespaces_followed=namespace
    )

    author = repo.provider_namespace.namespace.name

    notification = NotificationManger(
        email_template=author_release_template,
        preferences_name='notify_author_release',
        preferences_list=followers,
        subject='Ansible Galaxy: {} has released a new collection'.format(
            author
        ),
        db_message='New release from {}: {}'.format(
            author, repo.name
        ),
        repo=repo
    )

    path = '/{}/{}/'.format(author, repo.name)
    ctx = {
        'author_name': author,
        'content_name': repo.name,
        'content_url': notification.url + path,
    }

    notification.notify(ctx)


@celery.task
def new_survey(repo_id):
    repo = models.Repository.objects.get(id=repo_id)
    author = repo.provider_namespace.namespace.name
    owners = _get_preferences(repo.provider_namespace.namespace.owners.all())
    path = '/{}/{}/'.format(author, repo.name)

    notification = NotificationManger(
        email_template=new_survey_template,
        preferences_name='notify_survey',
        preferences_list=owners,
        subject='Ansible Galaxy: new survey for {}'.format(repo.name),
        db_message='New survey for {}'.format(repo.name),
        repo=repo
    )

    ctx = {
        'content_score': repo.community_score,
        'content_name': repo.name,
        'content_url': notification.url + path,
    }

    notification.notify(ctx)


def _get_preferences(users):
    preferences = []
    for user in users:
        # there isn't a guarantee that a user has a preferences object, so we
        # need to make sure to create one if they don't.
        obj, created = models.UserPreferences.objects.get_or_create(user=user)
        preferences.append(obj)

    return preferences


import_status_template = '''Hello,

This message is to notify you that a recent import of {content_name} on \
Ansible galaxy has {status}.

To see the import log, go to: {import_url}
'''


update_collection_template = '''Hello,

{namespace_name} has just released a new version of {content_name} on \
Ansible Galaxy.

To see the new version, visit {content_url}.
'''


author_release_template = '''Hello,

One of the author's ({author_name}) that you are following on Ansible Galaxy \
has just released a new collection named {content_name}.

To see it, visit {content_url}.
'''


new_survey_template = '''Hello,

Someone has just submitted a new survey for {content_name} on Ansible Galaxy. \
Your collection now has a user rating of {content_score}.

Visit {content_url} for more details.
'''


email_footer_template = '''
Cheers,
   Ansible Galaxy

-- To stop seeing these messages, visit {preferences_link} to update your \
settings.'''


email_verification_template = '''Hello,

You're receiving this e-mail because user {username} has give this address as \
an e-mail address to connect their account.

To confirm this is correct, go to {url}.

Cheers,
   Ansible Galaxy
'''
