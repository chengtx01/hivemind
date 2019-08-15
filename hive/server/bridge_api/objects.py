"""Handles building condenser-compatible response objects."""

import logging
import ujson as json

from hive.utils.normalize import sbd_amount, rep_to_raw
from hive.server.hive_api.objects import _follow_contexts

log = logging.getLogger(__name__)

async def load_accounts(db, names, observer_id=None):
    """`get_accounts`-style lookup for `get_state` compat layer."""
    sql = """SELECT id, name, display_name, about, reputation, vote_weight,
                    created_at, post_count, profile_image, location, website,
                    cover_image, rank
               FROM hive_accounts WHERE name IN :names"""
    rows = await db.query_all(sql, names=tuple(names))
    accounts = [_condenser_account_object(row) for row in rows]

    if observer_id:
        by_id = {account['id']: account for account in accounts}
        await _follow_contexts(db, by_id, observer_id, include_mute=True)

    return accounts

async def load_posts_reblogs(db, ids_with_reblogs, truncate_body=0):
    """Given a list of (id, reblogged_by) tuples, return posts w/ reblog key."""
    post_ids = [r[0] for r in ids_with_reblogs]
    reblog_by = dict(ids_with_reblogs)
    posts = await load_posts(db, post_ids, truncate_body=truncate_body)

    # Merge reblogged_by data into result set
    for post in posts:
        rby = set(reblog_by[post['post_id']].split(','))
        rby.discard(post['author'])
        if rby:
            post['reblogged_by'] = list(rby)

    return posts

async def load_posts_keyed(db, ids, truncate_body=0):
    """Given an array of post ids, returns full posts objects keyed by id."""
    assert ids, 'no ids passed to load_posts_keyed'

    # fetch posts and associated author reps
    sql = """SELECT post_id, author, permlink, title, body, category, depth,
                    promoted, payout, payout_at, is_paidout, children, votes,
                    created_at, updated_at, rshares, raw_json, json,
                    is_hidden, is_grayed, total_votes
               FROM hive_posts_cache WHERE post_id IN :ids"""
    result = await db.query_all(sql, ids=tuple(ids))
    author_reps = await _query_author_rep_map(db, result)

    posts_by_id = {}
    for row in result:
        row = dict(row)
        row['author_rep'] = author_reps[row['author']]
        post = _condenser_post_object(row, truncate_body=truncate_body)
        posts_by_id[row['post_id']] = post

    sql = """SELECT id, is_pinned, is_muted, is_valid
               FROM hive_posts WHERE id IN :ids"""
    for row in await db.query_all(sql, ids=tuple(ids)):
        if row['id'] in posts_by_id:
            post = posts_by_id[row['id']]
            post['stats']['is_pinned'] = row['is_pinned']
            post['stats']['is_muted'] = row['is_muted']
            post['stats']['is_valid'] = row['is_valid']

    return posts_by_id

async def load_posts(db, ids, truncate_body=0):
    """Given an array of post ids, returns full objects in the same order."""
    if not ids:
        return []

    # posts are keyed by id so we can return output sorted by input order
    posts_by_id = await load_posts_keyed(db, ids, truncate_body=truncate_body)

    # in rare cases of cache inconsistency, recover and warn
    missed = set(ids) - posts_by_id.keys()
    if missed:
        log.warning("get_posts do not exist in cache: %s", repr(missed))
        for _id in missed:
            ids.remove(_id)
            sql = ("SELECT id, author, permlink, depth, created_at, is_deleted "
                   "FROM hive_posts WHERE id = :id")
            post = await db.query_row(sql, id=_id)
            if not post['is_deleted']:
                # TODO: This should never happen. See #173 for analysis
                log.error("missing post: %s", dict(post))
            else:
                log.warning("requested deleted post: %s", dict(post))

    return [posts_by_id[_id] for _id in ids]

async def _query_author_rep_map(db, posts):
    """Given a list of posts, returns an author->reputation map."""
    if not posts: return {}
    names = tuple({post['author'] for post in posts})
    sql = "SELECT name, reputation FROM hive_accounts WHERE name IN :names"
    return {r['name']: r['reputation'] for r in await db.query_all(sql, names=names)}

def _condenser_account_object(row):
    """Convert an internal account record into legacy-steemd style."""
    return {
        'id': row['id'],
        'name': row['name'],
        'created': _json_date(row['created_at']),
        'post_count': row['post_count'],
        'reputation': rep_to_raw(row['reputation']),
        'stats': {
            'sp': int(row['vote_weight'] * 0.0005037),
            'rank': row['rank'],
        },
        'json_metadata': json.dumps({
            'profile': {'name': row['display_name'],
                        'about': row['about'],
                        'website': row['website'],
                        'location': row['location'],
                        'cover_image': row['cover_image'],
                        'profile_image': row['profile_image'],
                       }})}

def _condenser_post_object(row, truncate_body=0):
    """Given a hive_posts_cache row, create a legacy-style post object."""
    paid = row['is_paidout']

    # condenser#3424 mitigation
    if not row['category']:
        row['category'] = 'undefined'

    post = {}
    post['post_id'] = row['post_id']
    post['author'] = row['author']
    post['permlink'] = row['permlink']
    post['category'] = row['category']

    post['title'] = row['title']
    post['body'] = row['body'][0:truncate_body] if truncate_body else row['body']
    post['json_metadata'] = row['json']

    post['created'] = _json_date(row['created_at'])
    post['last_update'] = _json_date(row['updated_at'])
    post['depth'] = row['depth']
    post['children'] = row['children']
    post['net_rshares'] = row['rshares']

    post['last_payout'] = _json_date(row['payout_at'] if paid else None)
    post['cashout_time'] = _json_date(None if paid else row['payout_at'])
    post['total_payout_value'] = _amount(row['payout'] if paid else 0)
    post['curator_payout_value'] = _amount(0)
    post['pending_payout_value'] = _amount(0 if paid else row['payout'])
    post['promoted'] = "%.3f SBD" % row['promoted']

    post['replies'] = []
    post['body_length'] = len(row['body'])
    post['active_votes'] = _hydrate_active_votes(row['votes'])
    post['author_reputation'] = rep_to_raw(row['author_rep'])

    post['stats'] = {
        'hide': row['is_hidden'],
        'gray': row['is_grayed'],
        'total_votes': row['total_votes']}

    # import fields from legacy object
    assert row['raw_json']
    assert len(row['raw_json']) > 32
    raw_json = json.loads(row['raw_json'])

    if row['depth'] > 0:
        post['parent_author'] = raw_json['parent_author']
        post['parent_permlink'] = raw_json['parent_permlink']
    else:
        post['parent_author'] = ''
        post['parent_permlink'] = row['category']

    post['url'] = raw_json['url']
    post['root_title'] = raw_json['root_title']
    post['beneficiaries'] = raw_json['beneficiaries']
    post['max_accepted_payout'] = raw_json['max_accepted_payout']
    post['percent_steem_dollars'] = raw_json['percent_steem_dollars']

    if paid:
        curator_payout = sbd_amount(raw_json['curator_payout_value'])
        post['curator_payout_value'] = _amount(curator_payout)
        post['total_payout_value'] = _amount(row['payout'] - curator_payout)

    return post

def _amount(amount, asset='SBD'):
    """Return a steem-style amount string given a (numeric, asset-str)."""
    assert asset == 'SBD', 'unhandled asset %s' % asset
    return "%.3f SBD" % amount

def _hydrate_active_votes(vote_csv):
    """Convert minimal CSV representation into steemd-style object."""
    if not vote_csv: return []
    votes = []
    for line in vote_csv.split("\n"):
        voter, rshares, percent, reputation = line.split(',')
        votes.append(dict(voter=voter,
                          rshares=rshares,
                          percent=percent,
                          reputation=rep_to_raw(reputation)))
    return votes

def _json_date(date=None):
    """Given a db datetime, return a steemd/json-friendly version."""
    if not date: return '1969-12-31T23:59:59'
    return 'T'.join(str(date).split(' '))
