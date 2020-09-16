
import logging
import requests

from flask import current_app as app
from urllib.parse import urljoin

from apps.tasks import send_to
from superdesk import get_resource_service
from superdesk.lock import lock, unlock, touch
from superdesk.text_utils import get_text
from superdesk.celery_app import celery
from superdesk.editor_utils import Editor3Content


logger = logging.getLogger(__name__)

ULTRAD_ID = 'ultrad_id'
ULTRAD_URL = 'https://pc-trad.herokuapp.com/cms/'
ULTRAD_TIMEOUT = (5, 10)


class UltradException(RuntimeError):
    pass


def get_headers():
    return {'x-ultrad-auth': app.config['ULTRAD_AUTH']}


def upload_document(item):
    item_name = item.get('headline') or item.get('slugline')
    if not item_name or not item.get('body_html'):
        return

    payload = {
        'lang': {
            'fromLang': 'en',
            'toLang': 'fr',
        },
        'name': item_name,
        'state': 'new',
        'text': {
            'original': get_text(item['body_html']),
        },
    }

    resp = requests.post(ULTRAD_URL, json=payload, headers=get_headers(), timeout=ULTRAD_TIMEOUT)
    raise_for_resp_error(resp)
    data = get_json(resp)
    return data['_id']


def get_document(ultrad_id):
    url = urljoin(ULTRAD_URL, ultrad_id)
    resp = requests.get(url, headers=get_headers(), timeout=ULTRAD_TIMEOUT)
    raise_for_resp_error(resp)
    return get_json(resp)


def raise_for_resp_error(resp):
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        logger.error('HTTP error %d: %s when doing %s on %s',
                     resp.status_code,
                     resp.text,
                     resp.request.method,
                     resp.request.path_url)
        raise UltradException()


def get_json(resp):
    try:
        return resp.json()
    except ValueError:
        logger.error('error when parsing ultrad response "%s"', resp.text)
        raise UltradException()


@celery.task(soft_time_limit=300)
def sync():
    lock_name = 'ultrad'
    if not lock(lock_name):
        logger.info('lock taken %s', lock_name)
        return
    try:
        desk = get_resource_service('desks').find_one(req=None, name=app.config['ULTRAD_DESK'])
        if not desk:
            logger.warning('ultrad desk not found name=%s', app.config['ULTRAD_DESK'])
            return
        todo_stage = get_resource_service('stages').find_one(req=None, desk=desk['_id'],
                                                             name=app.config['ULTRAD_TODO_STAGE'])
        done_stage = get_resource_service('stages').find_one(req=None, desk=desk['_id'],
                                                             name=app.config['ULTRAD_DONE_STAGE'])
        if not todo_stage:
            logger.warning('ultrad todo stage is missing name=%s', app.config['ULTRAD_TODO_STAGE'])
            return
        if not done_stage:
            logger.warning('ultrad done stage is missing name=%s', app.config['ULTRAD_DONE_STAGE'])
            return
        lookup = {'task.stage': todo_stage['_id']}
        items = list(get_resource_service('archive').get(req=None, lookup=lookup))
        logger.info('checking %d items on ultrad', len(items))
        for item in items:
            if not touch(lock_name, expire=300):
                logger.warning('lost lock %s', lock_name)
                break
            if item.get('lock_user') and item.get('lock_session'):
                logger.info('skipping locked item guid=%s', item['guid'])
                continue
            try:
                ultrad_id = item['extra'][ULTRAD_ID]
            except KeyError:
                continue
            try:
                ultrad_doc = get_document(ultrad_id)
            except UltradException:
                continue
            if ultrad_doc['state'] == 'revised':
                try:
                    updated = item.copy()
                    updated['body_html'] = ultrad_doc['text']['edited']
                except KeyError:
                    logger.info('no content in ultrad for item guid=%s ultrad_id=%s', item['guid'], ultrad_id)
                    continue
                logger.info('updating item from ultrad guid=%s ultrad_id=%s', item['guid'], ultrad_id)
                editor = Editor3Content(updated)
                editor._create_state_from_html(updated['body_html'])
                editor.update_item()
                send_to(updated, desk_id=desk['_id'], stage_id=done_stage['_id'])
                updates = {
                    'task': updated['task'],
                    'body_html': updated['body_html'],
                    'fields_meta': updated['fields_meta'],
                }
                # don't use patch, it assumes there is a user
                get_resource_service('archive').update(item['_id'], updates, item)
                get_resource_service('archive').on_updated(updates, item)
            else:
                logger.debug('skip updating item guid=%s ultrad_id=%s state=%s',
                             item['guid'], ultrad_id, ultrad_doc['state'])
    finally:
        unlock(lock_name)
