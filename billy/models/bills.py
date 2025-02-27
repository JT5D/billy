import math
import operator
import collections
import itertools

from django.core import urlresolvers
import pyes
import pymongo

from billy.conf import settings
from billy.utils import parse_param_dt

from .base import (db, Document, RelatedDocument, RelatedDocuments,
                   ListManager, AttrManager, take)
from .metadata import Metadata
from .utils import CachedAttribute, mongoid_2_url

elasticsearch = pyes.ES(settings.ELASTICSEARCH_HOST,
                        settings.ELASTICSEARCH_TIMEOUT)


class Sponsor(dict):
    legislator = RelatedDocument('Legislator', instance_key='leg_id')


class SponsorsManager(AttrManager):

    def __iter__(self):
        '''Lazily fetch all legislator objects from the db.
        '''
        bill = self.bill
        sponsors = bill['sponsors']

        # Need to be able to set attributes on sponsor, a dict.
        dictwrapper = type('Sponsor', (dict,), {})

        if not hasattr(self, '_legislators'):
            # build a mapping from all _ids to legislators
            self._legislators = {}
            ids = filter(None, map(operator.itemgetter('leg_id'), sponsors))
            legislators = db.legislators.find({'_all_ids': {'$in': ids}})
            for obj in legislators:
                for _id in obj['_all_ids']:
                    self._legislators[_id] = obj

        for sponsor in sponsors:
            leg_id = sponsor['leg_id']
            if leg_id is not None and leg_id in self._legislators:
                legislator = self._legislators[sponsor['leg_id']]
                legislator.update(sponsor)
                legislator.bill = bill
                yield legislator
            else:
                spons = dictwrapper(sponsor)
                spons.bill = bill
                yield spons

    def primary_sponsors(self):
        'Return the primary sponsors on the bill.'
        for sponsor in self:
            if sponsor['type'] == 'primary':
                yield sponsor

    def first_primary(self):
        try:
            return next(self.primary_sponsors())
        except StopIteration:
            return

    def first(self):
        if self.bill['sponsors']:
            return next(iter(self))
        else:
            return None

    def excluding_first_primary(self):
        first = self.first_primary()
        sponsors = list(self)
        if '_id' in first:
            first_id = first['_id']
            for sp in sponsors:
                if first_id == sp['_id']:
                    sponsors.remove(sp)
                    break
        else:
            return sponsors
        return sponsors

    def first_fifteen(self):
        'views.bill'
        return take(15, self)

    def first_fifteen_remainder(self):
        len_ = len(self.document['sponsors'])
        if 15 < len_:
            return list(itertools.islice(self, 15))


class Action(dict):

    def actor_name(self):
        actor = self['actor']
        meta = self.bill.metadata
        for s in ('upper', 'lower'):
            if s in actor:
                chamber_name = meta['%s_chamber_name' % s]
                return actor.replace(s, chamber_name)
        return actor.title()

    @property
    def bill(self):
        return self.manager.document

    def action_display(self):
        '''The action text, with any hyperlinked related entities.'''
        action = self['action']
        annotations = []
        state = self.bill['state']
        if 'related_entities' in self:
            for entity in self['related_entities']:
                name = entity['name']
                _id = entity['id']

                # If the importer couldn't ID the entity,
                # skip.
                if _id is None:
                    continue
                url = mongoid_2_url(state, _id)
                link = '<a href="%s">%s</a>' % (url, name)
                if name in action:
                    action = action.replace(entity['name'], link)
                else:
                    annotations.append(link)
            if annotations:
                action += ' (%s)' % ', '.join(annotations)
        return action


class ActionsManager(ListManager):
    wrapper = Action

    def __iter__(self):
        for action in reversed(self.bill['actions']):
            yield self._wrapper(action)

    def _bytype(self, action_type, action_spec=None):
        '''Return the most recent date on which action_type occurred.
        Action spec is a dictionary of key-value attrs to match.'''
        for action in reversed(self.bill['actions']):
            if action_type in action['type']:
                for k, v in action_spec.items():
                    if action[k] == v:
                        yield action

    def _bytype_latest(self, action_type, action_spec=None):
        actions = self._bytype(action_type, action_spec)
        try:
            return next(actions)
        except StopIteration:
            return

    def latest_passed_upper(self):
        return self._bytype_latest('bill:passed', {'actor': 'upper'})

    def latest_passed_lower(self):
        return self._bytype_latest('bill:passed', {'actor': 'lower'})

    def latest_introduced_upper(self):
        return self._bytype_latest('bill:introduced', {'actor': 'upper'})

    def latest_introduced_lower(self):
        return self._bytype_latest('bill:introduced', {'actor': 'lower'})


class BillVote(Document):

    collection = db.votes

    bill = RelatedDocument('Bill')

    def _total_votes(self):
        return self['yes_count'] + self['no_count'] + self['other_count']

    def _ratio(self, key):
        '''Return the yes/total ratio as a percetage string
        suitable for use as as css attribute.'''
        total = float(self._total_votes())
        try:
            return math.floor(self[key] / total * 100)
        except ZeroDivisionError:
            return float(0)

    def yes_ratio(self):
        return self._ratio('yes_count')

    def no_ratio(self):
        return self._ratio('no_count')

    def other_ratio(self):
        return self._ratio('other_count')

    @property
    def quality_exceptions(self):
        exceptions = getattr(self, '_exceptions', None)
        if exceptions is None:
            self._exceptions = list(db.quality_exceptions.find({'ids':
                                                                self['_id']}))
        return self._exceptions

    @CachedAttribute
    def _legislator_objects(self):
        '''A cache of dereferenced legislator objects.
        '''
        kwargs = {}

        id_getter = operator.itemgetter('leg_id')
        ids = []
        for k in ('yes', 'no', 'other'):
            ids.extend(map(id_getter, self[k + '_votes']))
        objs = db.legislators.find({'_all_ids': {'$in': ids}}, **kwargs)

        # Handy to keep a reference to the vote on each legislator.
        objs = list(objs)
        id_cache = {}
        for obj in objs:
            obj.vote = self
            for _id in obj['_all_ids']:
                id_cache[_id] = obj

        return id_cache

    @CachedAttribute
    def legislator_vote_value(self):
        '''If this vote was accessed through the legislator.votes_manager,
        return the value of this legislator's vote.
        '''
        if not hasattr(self, 'legislator'):
            msg = ('legislator_vote_value can only be called '
                   'from a vote accessed by legislator.votes_manager.')
            raise ValueError(msg)
        leg_id = self.legislator.id
        for k in ('yes', 'no', 'other'):
            for leg in self[k + '_votes']:
                if leg['leg_id'] == leg_id:
                    return k

    def _vote_legislators(self, yes_no_other):
        '''Return all legislators who votes yes/no/other on this bill.
        '''
        #id_getter = operator.itemgetter('leg_id')
        #ids = map(id_getter, self['%s_votes' % yes_no_other])
        #return map(self._legislator_objects.get, ids)
        result = []
        for voter in self[yes_no_other + '_votes']:
            if voter['leg_id']:
                result.append(self._legislator_objects.get(voter['leg_id']))
            else:
                result.append(voter)
        return result

    def yes_vote_legislators(self):
        return self._vote_legislators('yes')

    def no_vote_legislators(self):
        return self._vote_legislators('no')

    def other_vote_legislators(self):
        return self._vote_legislators('other')

    def get_absolute_url(self):
        url = urlresolvers.reverse('vote', args=[self['state'], self['_id']])
        return url

    @CachedAttribute
    def is_probably_a_voice_vote(self):
        '''Guess whether this vote is a "voice vote".'''
        if '+voice_vote' in self:
            return True
        if '+vote_type' in self:
            if self['+vote_type'] == 'Voice':
                return True
        if 'voice vote' in self['motion'].lower():
            return True
        return False

    @CachedAttribute
    def has_votes(self):
        return 0 < sum([self['yes_count'], self['no_count'],
                        self['other_count']])

    @CachedAttribute
    def has_voters(self):
        return any([self['yes_votes'], self['no_votes'], self['other_votes']])

    def chamber_name(self):
        # Hack to accomodate bill-as-related-document and
        # inherited reverse lookup 'bill' attribute that exists
        # on vote objects returned from ListManager class.
        if callable(self.bill):
            bill = self.bill()
        else:
            bill = self.bill
        metadata = bill.metadata
        name = metadata['%s_chamber_name' % self['chamber']]
        return name


class Bill(Document):

    collection = db.bills
    instance_key = 'bill_id'

    sponsors_manager = SponsorsManager()
    actions_manager = ActionsManager()
    votes_manager = RelatedDocuments('BillVote', model_keys=['bill_id'],
                                     sort=[('date', pymongo.DESCENDING)])

    feed_entries = RelatedDocuments('FeedEntry', model_keys=['entity_ids'])

    @property
    def metadata(self):
        return Metadata.get_object(self['state'])

    def get_absolute_url(self):
        # canonical URL doesn't have spaces
        url = urlresolvers.reverse('bill',
                                   args=[self['state'], self['session'],
                                         self['bill_id'].replace(' ', '')])
        return url

    def display_name(self):
        return self['bill_id']

    def get_admin_url(self):
        return urlresolvers.reverse('bill', args=[self['state'], self.id])

    def session_details(self):
        metadata = self.metadata
        return metadata['session_details'][self['session']]

    def most_recent_action(self):
        if self['actions']:
            return self['actions'][-1]
        else:
            return {}

    def events(self):
        return db.events.find({"related_bills.bill_id": self['_id']})

    @property
    def chamber_name(self):
        '''"lower" --> "House of Representatives"'''
        return self.metadata['%s_chamber_name' % self['chamber']]

    @property
    def other_chamber(self):
        return {'upper': 'lower',
                'lower': 'upper'}[self['chamber']]

    @property
    def other_chamber_name(self):
        return self.metadata['%s_chamber_name' % self.other_chamber]

    def type_string(self):
        return self['type'][0]

    # Bill progress properties
    @property
    def actions_type_dict(self):
        typedict = getattr(self, '_actions_type_dict', None)
        if typedict is None:
            typedict = collections.defaultdict(list)
            for action in self['actions']:
                for type_ in action['type']:
                    typedict[type_].append(action)
            setattr(self, '_actions_type_dict', typedict)
        return dict(typedict)

    def date_introduced(self):
        '''Currently returns the earliest date the bill was introduced
        in either chamber.
        '''
        return self['action_dates']['first']

    def date_passed_lower(self):
        return self['action_dates']['passed_lower']

    def date_passed_upper(self):
        return self['action_dates']['passed_upper']

    def date_signed(self):
        return self['action_dates']['signed']

    def progress_data(self):
        data = [
            ('stage1', 'Introduced', 'date_introduced'),

            ('stage2',
             'Passed ' + self.chamber_name,
             'date_passed_' + self['chamber']),
        ]

        if 'lower_chamber_name' in self.metadata:
            data.append(('stage3',
                         'Passed ' + self.other_chamber_name,
                         'date_passed_' + self.other_chamber))
        data.append(('stage4', 'Governor Signs', 'date_signed'))

        for stage, text, method in data:
            yield stage, text, getattr(self, method)()

    @property
    def quality_exceptions(self):
        exceptions = getattr(self, '_exceptions', None)
        if exceptions is None:
            self._exceptions = list(db.quality_exceptions.find({'ids':
                                                                self['_id']}))
        return self._exceptions

    def _split_list(n, key):
        def first5(self):
            return self[key][:n]

        def remainder(self):
            if n < len(self[key]):
                remainder = self[key][n:]
            else:
                remainder = []
            return remainder
        return first5, remainder

    documents_preview, documents_remainder = _split_list(5, 'documents')
    versions_preview, versions_remainder = _split_list(12, 'versions')

    @staticmethod
    def search(query=None, state=None, chamber=None, subjects=None,
               bill_id=None, bill_id__in=None, search_window=None,
               updated_since=None, sponsor_id=None, bill_fields=None,
               status=None, type_=None, session=None):
        _filter = {}
        for key, value in [('state', state),
                            ('chamber', chamber),
                            ('subjects', subjects)]:
            if value is not None:
                _filter[key] = value

        if search_window:
            if search_window == 'session':
                _filter['_current_session'] = True
            elif search_window == 'term':
                _filter['_current_term'] = True
            elif search_window.startswith('session:'):
                _filter['session'] = search_window.split('session:')[1]
            elif search_window.startswith('term:'):
                _filter['_term'] = search_window.split('term:')[1]
            elif search_window == 'all':
                pass
            else:
                raise ValueError('invalid search_window. valid choices are '
                                 ' "term", "session", "all"')
        if updated_since:
            try:
                _filter['updated_at'] = {'$gte': parse_param_dt(updated_since)}
            except ValueError:
                raise ValueError('invalid updated_since parameter. '
                                 'please supply date in YYYY-MM-DD format')
        if sponsor_id:
            _filter['sponsors.leg_id'] = sponsor_id

        if status:
            # Status is slightly different: it's a dict like--
            # {'action_dates.signed': {'$ne': None}}
            _filter.update(**status)

        if type_:
            _filter['type'] = type_

        if session:
            _filter['session'] = session

        # process full-text query
        if query:
            # block spammers, possibly move to a BANNED_SEARCH_LIST setting
            if '<a href' in query:
                return db.bills.find({'state': None})

            query = {"query_string": {"fields": ["text", "title"],
                                      "default_operator": "AND",
                                      "query": query}}
            search = pyes.Search(query, fields=[])

            # take terms from mongo query
            es_terms = []
            if 'state' in _filter:
                es_terms.append(pyes.TermFilter('state',
                                                _filter.pop('state')))
            if 'session' in _filter:
                es_terms.append(pyes.TermFilter('session',
                                                _filter.pop('session')))
            if 'chamber' in _filter:
                es_terms.append(pyes.TermFilter('chamber',
                                                _filter.pop('chamber')))
            if 'subjects' in _filter:
                es_terms.append(pyes.TermFilter('subjects',
                                           _filter.pop('subjects')['$all']))
            if 'sponsors.leg_id' in _filter:
                es_terms.append(pyes.TermFilter('sponsors',
                                            _filter.pop('sponsors.leg_id')))

            # add terms
            if es_terms:
                search.filter = pyes.ANDFilter(es_terms)

            # page size is a guess, could use tweaks
            es_result = elasticsearch.search(search, search_type='scan',
                                             scroll='3m', size=250)
            doc_ids = [r.get_id() for r in es_result]
            _filter['versions.doc_id'] = {'$in': doc_ids}

        # return query
        return db.bills.find(_filter, bill_fields)
