import unittest

from django.contrib.auth.models import User
from django.core import mail
from django.db.utils import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

import pytz
from dateutil.parser import parse as parse_date
from rest_framework.test import APIClient
from wagtail.core.models import Page, Workflow, WorkflowTask, WorkflowState

from wagtail_review.models import (
    Comment, CommentReply, ExternalReviewer, Reviewer, ReviewTask, ReviewTaskState, Share)
from wagtail_review.token import Token

from .factories import CommentFactory, CommentReplyFactory, ReviewerFactory


class APITestCase(TestCase):
    fixtures = ['test.json']
    client_class = APIClient

    def setUp(self):
        self.reviewer = ReviewerFactory.create_external()

        page = Page.objects.get(id=2)
        self.page_revision = page.save_revision()
        self.other_page_revision = page.save_revision()

        other_page = Page.objects.get(id=1)
        self.other_page_page_revision = other_page.save_revision()

        Share.objects.create(
            page=Page.objects.get(id=2),
            external_user=self.reviewer.external,
            shared_by=User.objects.get(username="homer"),
            can_comment=True,
        )

        # Set up a workflow
        self.workflow = Workflow.objects.create()
        self.workflow_task = ReviewTask.objects.create()
        self.workflow_task.reviewers.add(self.reviewer)
        WorkflowTask.objects.create(workflow=self.workflow, task=self.workflow_task)


class TestHomeView(APITestCase):
    def test_get(self):
        response = self.client.get(reverse('wagtail_review:api:base'), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {
            'you': {
                'id': self.reviewer.id,
                'name': self.reviewer.get_name(),
            },
            'can_comment': True,
            'can_review': False,
        })

    def test_get_without_comments(self):
        Share.objects.update(can_comment=False)
        response = self.client.get(reverse('wagtail_review:api:base'), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertFalse(response.json()['can_comment'])
        self.assertFalse(response.json()['can_review'])

    def test_get_with_task_state(self):
        workflow_state = WorkflowState.objects.create(
            workflow=self.workflow,
            page=self.page_revision.page,
        )

        task_state = ReviewTaskState.objects.create(
            workflow_state=workflow_state,
            task=self.workflow_task,
            page_revision=self.page_revision,
        )

        workflow_state.current_task_state = task_state
        workflow_state.save(update_fields=['current_task_state'])

        response = self.client.get(reverse('wagtail_review:api:base'), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision, task_state).encode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertTrue(response.json()['can_comment'])
        self.assertTrue(response.json()['can_review'])


def check_comment_api_representation(self, comment, data):
    self.assertEqual(data['id'], comment.id)
    self.assertEqual(data['author'], {
        'id': comment.reviewer.id,
        'name': comment.reviewer.get_name()
    })
    self.assertEqual(data['quote'], comment.quote)
    self.assertEqual(data['text'], comment.text)
    self.assertEqual(parse_date(data['created_at']), comment.created_at)
    self.assertEqual(parse_date(data['updated_at']), comment.updated_at)
    self.assertIsNone(data['resolved_at'])
    self.assertEqual(data['content_path'], comment.content_path)
    self.assertEqual(data['start_xpath'], comment.start_xpath)
    self.assertEqual(data['start_offset'], comment.start_offset)
    self.assertEqual(data['end_xpath'], comment.end_xpath)
    self.assertEqual(data['end_offset'], comment.end_offset)
    self.assertEqual(data['replies'], [])


def check_comment_reply_api_representation(self, reply, data):
    self.assertEqual(data['id'], reply.id)
    self.assertEqual(data['author'], {
        'id': reply.reviewer.id,
        'name': reply.reviewer.get_name()
    })
    self.assertEqual(data['text'], reply.text)
    self.assertEqual(parse_date(data['created_at']), reply.created_at)
    self.assertEqual(parse_date(data['updated_at']), reply.updated_at)


class TestCommentListView(APITestCase):
    def setUp(self):
        super().setUp()

        self.comment_a = CommentFactory.create(page_revision=self.page_revision)
        self.comment_b = CommentFactory.create(page_revision=self.page_revision)
        self.comment_on_another_revision = CommentFactory.create(page_revision=self.other_page_revision)
        self.comment_on_another_page = CommentFactory.create(page_revision=self.other_page_page_revision)

    def test_get_comments(self):
        response = self.client.get(reverse('wagtail_review:api:comment_list'), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(set(comment['id'] for comment in response.json()), {self.comment_a.id, self.comment_b.id})

        check_comment_api_representation(self, self.comment_a, response.json()[0])

    def test_post_new_comment(self):
        post_data = {
            'quote': "This is a test",
            'text': "blah blah blah",
            'content_path': "title",
            'start_xpath': "/foo",
            'start_offset': 1,
            'end_xpath': "/foo",
            'end_offset': 10,
        }

        response = self.client.post(reverse('wagtail_review:api:comment_list'), post_data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response['Content-Type'], 'application/json')

        comment = Comment.objects.get(text="blah blah blah")

        check_comment_api_representation(self, comment, response.json())

        self.assertEqual(comment.page_revision, self.page_revision)
        self.assertEqual(comment.reviewer, self.reviewer)


class TestCommentView(APITestCase):
    def setUp(self):
        super().setUp()

        self.comment = CommentFactory.create(page_revision=self.page_revision, reviewer=self.reviewer)
        self.comment_from_another_reviewer = CommentFactory.create(page_revision=self.page_revision)
        self.comment_on_another_revision = CommentFactory.create(page_revision=self.other_page_revision)

    def test_get_comment(self):
        response = self.client.get(reverse('wagtail_review:api:comment', args=[self.comment.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')

        check_comment_api_representation(self, self.comment, response.json())

    def test_get_comment_thats_not_on_current_revision(self):
        response = self.client.get(reverse('wagtail_review:api:comment', args=[self.comment_on_another_revision.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'detail': 'Not found.'})

    def get_update_comment_data(self):
        return {
            'quote': self.comment.quote,
            'text': "This is the new text",
            'content_path': self.comment.content_path,
            'start_xpath': self.comment.start_xpath,
            'start_offset': self.comment.start_offset,
            'end_xpath': self.comment.end_xpath,
            'end_offset': self.comment.end_offset,
        }

    def test_update_comment(self):
        data = self.get_update_comment_data()
        previous_updated_at = self.comment.updated_at

        response = self.client.put(reverse('wagtail_review:api:comment', args=[self.comment.id]), data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')

        self.comment.refresh_from_db()
        self.assertEqual(self.comment.text, "This is the new text")
        self.assertNotEqual(self.comment.updated_at, previous_updated_at)

    def test_cant_update_comment_from_another_reviewer(self):
        data = self.get_update_comment_data()

        response = self.client.put(reverse('wagtail_review:api:comment', args=[self.comment_from_another_reviewer.id]), data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'detail': 'You do not have permission to perform this action.'})

    def test_cant_update_comment_on_another_revision(self):
        data = self.get_update_comment_data()

        response = self.client.put(reverse('wagtail_review:api:comment', args=[self.comment_on_another_revision.id]), data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'detail': 'Not found.'})

    def test_delete_comment(self):
        response = self.client.delete(reverse('wagtail_review:api:comment', args=[self.comment.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Comment.objects.filter(id=self.comment.id).exists())

    def test_cant_delete_comment_from_another_reviewer(self):
        response = self.client.delete(reverse('wagtail_review:api:comment', args=[self.comment_from_another_reviewer.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'detail': 'You do not have permission to perform this action.'})

    def test_cant_delete_comment_on_another_revision(self):
        response = self.client.delete(reverse('wagtail_review:api:comment', args=[self.comment_on_another_revision.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'detail': 'Not found.'})


class TestCommentResolvedView(APITestCase):
    def setUp(self):
        super().setUp()

        self.comment = CommentFactory.create(page_revision=self.page_revision, reviewer=self.reviewer)
        self.comment_from_another_reviewer = CommentFactory.create(page_revision=self.page_revision)
        self.comment_on_another_revision = CommentFactory.create(page_revision=self.other_page_revision)

    def test_resolve_comment(self):
        response = self.client.put(reverse('wagtail_review:api:comment_resolved', args=[self.comment.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)

    def test_can_resolve_comment_from_another_reviewer(self):
        response = self.client.put(reverse('wagtail_review:api:comment_resolved', args=[self.comment_from_another_reviewer.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)

    def test_cant_resolve_comment_on_another_revision(self):
        response = self.client.put(reverse('wagtail_review:api:comment_resolved', args=[self.comment_on_another_revision.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'detail': 'Not found.'})

    def test_unresolve_comment(self):
        response = self.client.delete(reverse('wagtail_review:api:comment_resolved', args=[self.comment.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)

    def test_can_unresolve_comment_from_another_reviewer(self):
        response = self.client.delete(reverse('wagtail_review:api:comment_resolved', args=[self.comment_from_another_reviewer.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)

    def test_cant_unresolve_comment_on_another_revision(self):
        response = self.client.delete(reverse('wagtail_review:api:comment_resolved', args=[self.comment_on_another_revision.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'detail': 'Not found.'})


class TestCommentReplyListView(APITestCase):
    def setUp(self):
        super().setUp()

        self.comment = CommentFactory.create(page_revision=self.page_revision)
        self.other_comment = CommentFactory.create(page_revision=self.page_revision)

        self.reply = CommentReplyFactory.create(comment=self.comment, reviewer=self.reviewer)
        self.reply_from_other_reviewer = CommentReplyFactory.create(comment=self.comment)
        self.reply_on_another_comment = CommentReplyFactory.create(comment=self.other_comment, reviewer=self.reviewer)

    def test_get_replies(self):
        response = self.client.get(reverse('wagtail_review:api:commentreply_list', args=[self.comment.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(set(comment['id'] for comment in response.json()), {self.reply.id, self.reply_from_other_reviewer.id})

        check_comment_reply_api_representation(self, self.reply, response.json()[0])

    def test_post_new_reply(self):
        post_data = {
            'text': "blah blah blah",
        }

        response = self.client.post(reverse('wagtail_review:api:commentreply_list', args=[self.comment.id]), post_data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response['Content-Type'], 'application/json')

        reply = CommentReply.objects.get(text="blah blah blah")

        check_comment_reply_api_representation(self, reply, response.json())

        self.assertEqual(reply.comment, self.comment)
        self.assertEqual(reply.reviewer, self.reviewer)


class TestCommentReplyView(APITestCase):
    def setUp(self):
        super().setUp()

        self.comment = CommentFactory.create(page_revision=self.page_revision)
        self.other_comment = CommentFactory.create(page_revision=self.page_revision)

        self.reply = CommentReplyFactory.create(comment=self.comment, reviewer=self.reviewer)
        self.reply_from_other_reviewer = CommentReplyFactory.create(comment=self.comment)

    def test_get_reply(self):
        response = self.client.get(reverse('wagtail_review:api:commentreply', args=[self.comment.id, self.reply.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')

        check_comment_reply_api_representation(self, self.reply, response.json())

    def test_get_reply_comment_id_must_be_correct_in_url(self):
        response = self.client.get(reverse('wagtail_review:api:commentreply', args=[self.other_comment.id, self.reply.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'detail': 'Not found.'})

    def test_update_reply(self):
        data = {
            'text': "This is the new text",
        }
        previous_updated_at = self.reply.updated_at

        response = self.client.put(reverse('wagtail_review:api:commentreply', args=[self.comment.id, self.reply.id]), data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')

        self.reply.refresh_from_db()
        self.assertEqual(self.reply.text, "This is the new text")
        self.assertNotEqual(self.reply.updated_at, previous_updated_at)

    def test_delete_reply(self):
        response = self.client.delete(reverse('wagtail_review:api:commentreply', args=[self.comment.id, self.reply.id]), HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision).encode())

        self.assertEqual(response.status_code, 204)
        self.assertFalse(CommentReply.objects.filter(id=self.reply.id).exists())


class TestRespondView(APITestCase):
    def setUp(self):
        super().setUp()

        self.workflow_state = WorkflowState.objects.create(
            workflow=self.workflow,
            page=self.page_revision.page,
        )

        self.task_state = ReviewTaskState.objects.create(
            workflow_state=self.workflow_state,
            task=self.workflow_task,
            page_revision=self.page_revision,
        )

        self.workflow_state.current_task_state = self.task_state
        self.workflow_state.save(update_fields=['current_task_state'])

    def test_post_approved_response(self):
        post_data = {
            'taskAction': 'approve',
            'comment': "This is the comment",
        }
        response = self.client.post(reverse('wagtail_review:api:respond'), post_data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision, self.task_state).encode())

        self.assertEqual(response.status_code, 200)

        self.task_state.refresh_from_db()
        self.assertEqual(self.task_state.reviewer, self.reviewer)
        self.assertTrue(self.task_state.finished_at)
        self.assertEqual(self.task_state.status, 'approved')
        self.assertEqual(self.task_state.comment, "This is the comment")

    def test_post_needs_changes_response(self):
        post_data = {
            'taskAction': 'reject',
            'comment': "This is the comment",
        }
        response = self.client.post(reverse('wagtail_review:api:respond'), post_data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision, self.task_state).encode())

        self.assertEqual(response.status_code, 200)

        self.task_state.refresh_from_db()
        self.assertEqual(self.task_state.reviewer, self.reviewer)
        self.assertTrue(self.task_state.finished_at)
        self.assertEqual(self.task_state.status, 'rejected')
        self.assertEqual(self.task_state.comment, "This is the comment")

    def test_post_invalid_action(self):
        post_data = {
            'taskAction': 'foo',
            'comment': "This is the comment",
        }
        response = self.client.post(reverse('wagtail_review:api:respond'), post_data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision, self.task_state).encode())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'detail': 'You do not have permission to perform this action.'})

    def test_post_review_token_for_different_task(self):
        # This may happen if someone uses an old token (clicked from email history) and the page happens
        # to be in another review task
        past_workflow_state = WorkflowState.objects.create(
            workflow=self.workflow,
            page=self.page_revision.page,
            status=WorkflowState.STATUS_APPROVED,
        )

        past_task_state = ReviewTaskState.objects.create(
            workflow_state=past_workflow_state,
            task=self.workflow_task,
            page_revision=self.page_revision,
            status=ReviewTaskState.STATUS_APPROVED,
        )

        past_workflow_state.current_task_state = past_task_state
        past_workflow_state.save(update_fields=['current_task_state'])

        post_data = {
            'taskAction': 'approve',
            'comment': "This is the comment",
        }
        response = self.client.post(reverse('wagtail_review:api:respond'), post_data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision, past_task_state).encode())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'detail': "The provided token isn't for the current task."})

    @unittest.expectedFailure  # No validation yet
    def test_post_long_comment(self):
        post_data = {
            'taskAction': 'approve',
            'comment': "A" * 201,
        }
        response = self.client.post(reverse('wagtail_review:api:respond'), post_data, HTTP_X_REVIEW_TOKEN=Token(self.reviewer, self.page_revision, self.task_state).encode())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'comment': ['TODO']})
