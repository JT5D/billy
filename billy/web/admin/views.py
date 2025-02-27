import re
import json
import time
import types
import urllib
import random
import pymongo
import decimal
import functools
import unicodecsv
import datetime
import urlparse

from bson import ObjectId
from json import JSONEncoder

from operator import itemgetter
from itertools import chain, imap
from collections import defaultdict, OrderedDict

from django.core import urlresolvers
from django.shortcuts import render, redirect
from django.http import Http404, HttpResponse
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods

from billy import db
from billy.conf import settings
from billy.utils import metadata, find_bill
from billy.scrape import JSONDateEncoder
from billy.importers.utils import merge_legislators
from billy.importers.legislators import deactivate_legislators
from billy.reports.utils import get_quality_exceptions, QUALITY_EXCEPTIONS


def _meta_and_report(abbr):
    meta = metadata(abbr)
    if not meta:
        raise Http404('No metadata found for abbreviation %r.' % abbr)
    report = db.reports.find_one({'_id': abbr})
    if not report:
        raise Http404('No reports found for abbreviation %r.' % abbr)
    return meta, report


def keyfunc(obj):
    try:
        return int(obj['district'])
    except ValueError:
        return obj['district']


@login_required
def _csv_response(request, csv_name, columns, data, abbr):
    if 'csv' in request.REQUEST:
        resp = HttpResponse(mimetype="text/plain")
        resp['Content-Disposition'] = 'attachment; filename=%s_%s.csv' % (
            abbr, csv_name)
        out = unicodecsv.writer(resp)
        for item in data:
            out.writerow(item)
        return resp
    else:
        return render(request, 'billy/generic_table.html',
                      {'columns': columns,
                       'data': data, 'metadata': metadata(abbr)})


@login_required
def browse_index(request, template='billy/index.html'):
    rows = []

    spec = {}
    only = request.GET.get('only', [])
    if only:
        spec = {'_id': {'$in': only.split(',')}}

    for report in db.reports.find(spec):
        report['id'] = report['_id']
        meta = db.metadata.find_one({'_id': report['_id']})
        report['name'] = meta['name']
        report['unicameral'] = ('lower_chamber_name' not in meta)
        report['influence_explorer'] = ('influenceexplorer' in
                                        meta['feature_flags'])
        report['bills']['typed_actions'] = (100 -
            report['bills']['actions_per_type'].get('other', 100))
        rows.append(report)

    rows.sort(key=lambda x: x['name'])

    return render(request, template, {'rows': rows})


@login_required
def overview(request, abbr):
    meta, report = _meta_and_report(abbr)
    context = {}
    context['metadata'] = meta
    context['report'] = report
    context['sessions'] = db.bills.find({'state': abbr}).distinct('session')

    def _add_time_delta(runlog):
        time_delta = runlog['scraped']['ended'] - runlog['scraped']['started']
        runlog['scraped']['time_delta'] = datetime.timedelta(time_delta.days,
                                                         time_delta.seconds)
    try:
        runlog = db.billy_runs.find({
            "scraped.state": abbr
        }).sort("scraped.started", direction=pymongo.DESCENDING)[0]
        _add_time_delta(runlog)
        context['runlog'] = runlog

        if runlog.get('failure'):
            last_success = db.billy_runs.find({
                "scraped.state": abbr,
                "failure": None,
            }).sort("scraped.started", direction=pymongo.DESCENDING)[0]
            _add_time_delta(last_success)
            context['last_success'] = last_success
    except IndexError:
        runlog = False

    return render(request, 'billy/state_index.html', context)


@login_required
def metadata_json(request, abbr):
    re_attr = re.compile(r'^    "(.{1,100})":', re.M)
    obj = metadata(abbr)
    obj_json = json.dumps(obj, indent=4, cls=JSONDateEncoder)

    def subfunc(m, tmpl='    <a name="%s">%s:</a>'):
        val = m.group(1)
        return tmpl % (val, val)

    for k in obj:
        obj_json = re_attr.sub(subfunc, obj_json)

    tmpl = '<a href="{0}">{0}</a>'
    obj_json = re.sub('"(http://.+?)"',
                      lambda m: tmpl.format(*m.groups()), obj_json)
    context = {'metadata': obj,
               'keys': sorted(obj),
               'metadata_json': obj_json}
    return render(request, 'billy/metadata_json.html', context)


@login_required
def run_detail_graph_data(request, abbr):

    def rolling_average(oldAverage, newItem, oldAverageCount):
        """
        Simple, unweighted rolling average. If you don't get why we have
        to factor the oldAverageCount back in, it's because new values will
        have as much weight as the last sum combined if you put it over 2.
        """
        return float(
            (newItem + (oldAverageCount * (oldAverage))) /
                        (oldAverageCount + 1))

    def _do_pie(runs):
        excs = {}
        for run in runs:
            if "failure" in run:
                for r in run['scraped']['run_record']:
                    if "exception" in r:
                        ex = r['exception']
                        try:
                            excs[ex['type']] += 1
                        except KeyError:
                            excs[ex['type']] = 1
        ret = []
        for l in excs:
            ret.append([l, excs[l]])
        return ret

    def _do_stacked(runs):
        fields = ["legislators", "bills", "votes", "committees"]
        ret = {}
        for field in fields:
            ret[field] = []

        for run in runs:
            guy = run['scraped']['run_record']
            for field in fields:
                try:
                    g = None
                    for x in guy:
                        if x['type'] == field:
                            g = x
                    if not g:
                        raise KeyError("Missing kruft")

                    delt = (g['end_time'] - g['start_time']).total_seconds()
                    ret[field].append(delt)
                except KeyError:        # XXX: THIS MESSES STUFF UP. REVISE.
                    ret[field].append(0)
        l = []
        for line in fields:
            l.append(ret[line])
        return l

    def _do_digest(runs):
        oldAverage = 0
        oldAverageCount = 0
        data = {"runs": [], "avgs": [], "stat": []}
        for run in runs:
            timeDelta = (
                run['scraped']['ended'] - run['scraped']['started']
            ).total_seconds()
            oldAverage = rolling_average(oldAverage, timeDelta,
                                         oldAverageCount)
            oldAverageCount += 1
            stat = "Failure" if "failure" in run else ""

            s = time.mktime(run['scraped']['started'].timetuple())

            data['runs'].append([s, timeDelta, stat])
            data['avgs'].append([s, oldAverage, ''])
            data['stat'].append(stat)
        return data
    history_count = 50

    default_spec = {"scraped.state": abbr}
    data = {"lines": {}, "pies": {}, "stacked": {}, "title": {}}

    speck = {
        "default-stacked": {"run": _do_stacked,
            "title": "Last %s runs" % (history_count),
            "type": "stacked",
            "spec": {}
        },
        #"default": {"run": _do_digest,
        #    "title": "Last %s runs" % (history_count),
        #    "type": "lines",
        #    "spec": {}
        #},
        #"clean": {"run": _do_digest,
        #    "title": "Last %s non-failed runs" % (history_count),
        #    "type": "lines",
        #    "spec": {
        #        "failure": {"$exists": False }
        #    }
        #},
        #"failure": {"run": _do_digest,
        #    "title": "Last %s failed runs" % (history_count),
        #    "type": "lines",
        #    "spec": {
        #        "failure": {"$exists": True  }
        #    }
        #},
        "falure-pie": {"run": _do_pie,
            "title": "Digest of what exceptions have been thrown",
            "type": "pies",
            "spec": {
                "failure": {"$exists": True}
            }
        },
    }

    for line in speck:
        query = speck[line]["spec"].copy()
        query.update(default_spec)
        runs = db.billy_runs.find(query).sort(
            "scrape.start", direction=pymongo.ASCENDING)[:history_count]
        data[speck[line]['type']][line] = speck[line]["run"](runs)
        data['title'][line] = speck[line]['title']

    return HttpResponse(
        json.dumps(data, cls=JSONDateEncoder),
        #content_type="text/json"
        content_type="text/plain")


@login_required
def run_detail(request, obj=None):
    try:
        run = db.billy_runs.find({
            "_id": ObjectId(obj)
        })[0]
    except IndexError as e:
        return render(request, 'billy/run_empty.html', {
            "warning": "No records exist. Fetch returned a(n) %s" % (
                    e.__class__.__name__)})
    return render(request, 'billy/run_detail.html', {
        "run": run,
        "metadata": {"abbreviation": run['state'], "name": run['state']}
    })


@login_required
def state_run_detail(request, abbr):
    try:
        allruns = db.billy_runs.find({
            "scraped.state": abbr
        }).sort("scraped.started", direction=pymongo.DESCENDING)[:25]
        runlog = allruns[0]
    except IndexError as e:
        return render(request, 'billy/run_empty.html', {
            "warning": "No records exist. Fetch returned a(n) %s" % (
                    e.__class__.__name__)})

    # pre-process goodies for the template
    runlog['scraped']['t_delta'] = (
        runlog['scraped']['ended'] - runlog['scraped']['started'])
    for entry in runlog['scraped']['run_record']:
        if not "exception" in entry:
            entry['t_delta'] = (
                entry['end_time'] - entry['start_time'])

    context = {"runlog": runlog, "allruns": allruns, "state": abbr,
               "metadata": metadata(abbr)}

    if "failure" in runlog:
        context["alert"] = dict(type='error',
                                title="Exception during Execution",
                                message="""
This build had an exception during it's execution. Please check below
for the exception and error message.
""")

    return render(request, 'billy/state_run_detail.html', context)


@never_cache
@login_required
def bills(request, abbr):
    meta, report = _meta_and_report(abbr)

    terms = list(chain.from_iterable(map(itemgetter('sessions'),
                                         meta['terms'])))

    def sorter(item, index=terms.index, len_=len(terms)):
        '''Sort session strings in order described in state's metadata.'''
        session, data = item
        return index(session)

    # Convert sessions into an ordered dict.
    sessions = report['bills']['sessions']
    sessions = sorted(sessions.items(), key=sorter)
    sessions = OrderedDict(sessions)

    def decimal_format(value, TWOPLACES=decimal.Decimal(100) ** -1):
        '''Format a float like 2.2345123 as a decimal like 2.23'''
        n = decimal.Decimal(str(value))
        n = n.quantize(TWOPLACES)
        return unicode(n)

    # Define data for the tables for counts, types, etc.
    tablespecs = [
        ('Bill Counts', {'rownames': ['upper_count', 'lower_count',
                                      'version_count']}),

        ('Bill Types', {
            'keypath': ['bill_types'],
                'summary': {
                'object_type': 'bills',
                'key': 'type',
                },
            }),

        ('Actions by Type', {
            'keypath': ['actions_per_type'],
            'summary': {
                'object_type': 'actions',
                'key': 'type',
                },
            }),

        ('Actions by Actor', {
            'keypath': ['actions_per_actor'],
            'summary': {
                'object_type': 'actions',
                'key': 'actor',
                },
            }),

        ('Quality Issues',   {'rownames': [
                                'sponsorless_count', 'actionless_count',
                                'actions_unsorted', 'bad_vote_counts',
                                'version_count', 'versionless_count',

                                'sponsors_with_id',
                                'rollcalls_with_leg_id',
                                'have_subjects',
                                'updated_this_year',
                                'updated_this_month',
                                'updated_today',
                                'vote_passed']}),
        ]

    format_as_percent = [
        'sponsors_with_id',
        'rollcalls_with_leg_id',
        'have_subjects',
        'updated_this_year',
        'updated_this_month',
        'updated_today',
        'actions_per_actor',
        'actions_per_type']

    # Create the data for each table.
    tables = []
    for name, spec in tablespecs:
        column_names = []
        rows = defaultdict(list)
        href_params = {}
        tabledata = {'abbr': abbr,
                     'title': name,
                     'column_names': column_names,
                     'rows': rows}
        contexts = []

        for session, context in sessions.items():
            column_names.append(session)
            if 'keypath' in spec:
                for k in spec['keypath']:
                    context = context[k]
            contexts.append(context)

        try:
            rownames = spec['rownames']
        except KeyError:
            rownames = reduce(lambda x, y: set(x) | set(y), contexts)

        for context in contexts:
            for r in rownames:

                val = context.get(r, 0)
                if not isinstance(val, (int, float, decimal.Decimal)):
                    val = len(val)

                use_percent = any([
                    r in format_as_percent,
                    name in ['Actions by Actor', 'Actions by Type'],
                    ])

                if use_percent and (val != 0):
                    val = decimal_format(val)
                    val += ' %'
                rows[r].append(val)

                # Link to summary/distinct views.
                if 'summary' in spec:

                    try:
                        spec_val = spec['spec'](r)
                    except KeyError:
                        spec_val = r
                    else:
                        spec_val = json.dumps(spec_val, cls=JSONDateEncoder)

                    params = dict(spec['summary'], session=session,
                                  val=spec_val)

                    params = urllib.urlencode(params)
                    href_params[r] = params

        # Add the final "total" column.
        tabledata['column_names'].append('Total')
        for k, v in rows.items():
            try:
                sum_ = sum(v)
            except TypeError:
                sum_ = 'n/a'
            v.append(sum_)

        rowdata = [((r, href_params.get(r)), cells)
                   for (r, cells) in rows.items()]
        tabledata['rowdata'] = rowdata

        tables.append(tabledata)

    # ------------------------------------------------------------------------
    # Render the tables.
    _render = functools.partial(render_to_string, 'billy/bills_table.html')
    tables = map(_render, tables)

    return render(request, "billy/bills.html",
                  dict(tables=tables, metadata=meta, sessions=sessions,
                       tablespecs=tablespecs))


@login_required
def summary_index(request, abbr, session):

    object_types = 'votes actions versions sponsors documents sources'.split()

    def build(context_set):
        summary = defaultdict(int)
        for c in context_set:
            for k, v in c.items():
                summary[k] += 1
        return dict(summary)

    def build_state(abbr):

        bills = list(db.bills.find({'state': abbr, 'session': session}))
        res = {}
        for k in object_types:
            res[k] = build(chain.from_iterable(map(itemgetter(k), bills)))

        res.update(bills=build(bills))

        return res
    summary = build_state(abbr)

    return render(request, 'billy/summary_index.html', {'summary': summary})


@login_required
def summary_object_key(request, abbr, urlencode=urllib.urlencode,
                       collections=("bills", "legislators", "committees"),
                       dumps=json.dumps, Decimal=decimal.Decimal):
    session = request.GET['session']
    object_type = request.GET['object_type']
    key = request.GET['key']
    spec = {'state': abbr, 'session': session}

    if object_type in collections:
        collection = getattr(db, object_type)
        fields_key = key
        objs = collection.find(spec, {fields_key: 1})
        objs = imap(itemgetter(key), objs)
    else:
        collection = db.bills
        fields_key = '%s.%s' % (object_type, key)
        objs = collection.find(spec, {fields_key: 1})
        objs = imap(itemgetter(object_type), objs)

        def get_objects(objs):
            for _list in objs:
                for _obj in _list:
                    try:
                        yield _obj[key]
                    except KeyError:
                        pass
        objs = get_objects(objs)

    objs = (dumps(obj, cls=JSONDateEncoder) for obj in objs)
    counter = defaultdict(Decimal)
    for obj in objs:
        counter[obj] += 1

    params = lambda val: urlencode(dict(object_type=object_type,
                                        key=key, val=val, session=session))

    total = len(counter)
    objs = sorted(counter, key=counter.get, reverse=True)
    objs = ((obj, counter[obj], counter[obj] / total, params(obj))
            for obj in objs)
    return render(request, 'billy/summary_object_key.html', locals())


@login_required
def summary_object_key_vals(request, abbr, urlencode=urllib.urlencode,
                        collections=("bills", "legislators", "committees")):
    meta = metadata(abbr)
    session = request.GET['session']
    object_type = request.GET['object_type']
    key = request.GET['key']

    val = request.GET['val']
    try:
        val = json.loads(val)
    except ValueError:
        pass

    spec = {'state': abbr, 'session': session}
    fields = {'_id': 1}

    if object_type in collections:
        spec.update({key: val})
        objects = getattr(db, object_type).find(spec, fields)
        objects = ((object_type, obj['_id']) for obj in objects)
    else:
        spec.update({'.'.join([object_type, key]): val})
        objects = db.bills.find(spec, fields)
        objects = (('bills', obj['_id']) for obj in objects)

    spec = json.dumps(spec, cls=JSONDateEncoder, indent=4)

    return render(request, 'billy/summary_object_keyvals.html', dict(
        object_type=object_type,
        objects=objects,
        spec=spec,
        meta=meta
        ))


@login_required
def object_json(request, collection, _id,
                re_attr=re.compile(r'^    "(.{1,100})":', re.M)):

    class MongoEncoder(JSONEncoder):

        def default(self, obj, **kwargs):
            if isinstance(obj, ObjectId):
                return str(obj)
            elif isinstance(obj, datetime.datetime):
                return time.mktime(obj.utctimetuple())
            elif isinstance(obj, datetime.date):
                return time.mktime(obj.timetuple())
            else:
                return JSONEncoder.default(obj, **kwargs)

    obj = getattr(db, collection).find_one(_id)
    obj_json = json.dumps(obj, cls=MongoEncoder, indent=4)
    obj_isbill = (obj['_type'] == 'bill')
    obj_url = None

    if obj_isbill:
        try:
            obj_url = obj['sources'][0]['url']
        except:
            pass

    obj_id = obj['_id']
    obj_json = json.dumps(obj, cls=MongoEncoder, indent=4)

    def subfunc(m, tmpl='    <a name="%s">%s:</a>'):
        val = m.group(1)
        return tmpl % (val, val)

    for k in obj:
        obj_json = re_attr.sub(subfunc, obj_json)

    tmpl = '<a href="{0}">{0}</a>'
    obj_json = re.sub('"(http://.+?)"',
                      lambda m: tmpl.format(*m.groups()), obj_json)

    return render(request, 'billy/object_json.html', dict(
        obj=obj, obj_id=obj_id, obj_json=obj_json, obj_url=obj_url))


@login_required
def other_actions(request, abbr):
    report = db.reports.find_one({'_id': abbr})
    if not report:
        raise Http404('No reports found for abbreviation %r.' % abbr)
    return _csv_response(request, 'other_actions', ('action', '#'),
                         sorted(report['bills']['other_actions']), abbr)


@login_required
def duplicate_versions(request, abbr):
    meta, report = _meta_and_report(abbr)

    return render(request, "billy/duplicate_versions.html",
                  {'metadata': meta, 'report': report})


def _bill_spec(meta, limit):
    abbr = meta['abbreviation']

    # basic spec
    latest_session = meta['terms'][-1]['sessions'][-1]
    spec = {settings.LEVEL_FIELD: abbr.lower(), 'session': latest_session}

    basic_specs = {
        "no_versions": {'versions': []},
        "no_sponsors": {'sponsors': []},
        "no_actions": {'actions': []}
    }

    if limit in basic_specs:
        spec.update(basic_specs[limit])
        spec.pop('session')     # all sessions
    elif limit == 'current_term':
        curTerms = meta['terms'][-1]['sessions']
        spec['session'] = {"$in": curTerms}
    elif limit == '':
        pass
    else:
        raise ValueError('invalid limit: {0}'.format(limit))

    return spec


@never_cache
@login_required
def random_bill(request, abbr):
    meta = metadata(abbr)
    if not meta:
        raise Http404

    spec = _bill_spec(meta, request.GET.get('limit', ''))
    bills = db.bills.find(spec)

    count = bills.count()
    if count:
        bill = bills[random.randint(0, count - 1)]
        warning = None
    else:
        bill = None
        warning = 'No bills matching the criteria were found.'

    try:
        bill_id = bill['_id']
    except TypeError:
        # Bill was none (see above).
        bill_id = None

    context = {'bill': bill, 'id': bill_id, 'random': True,
               'state': abbr.lower(), 'warning': warning, 'metadata': meta}

    return render(request, 'billy/bill.html', context)


@login_required
def bill_list(request, abbr):
    meta = metadata(abbr)
    if not meta:
        raise Http404('No metadata found for abbreviation %r' % abbr)

    if 'version_url' in request.GET:
        version_url = request.GET.get('version_url')
        spec = {'versions.url': version_url}
        exceptions = []
    else:
        limit = request.GET.get('limit', '')
        exceptions = get_quality_exceptions(abbr)['bills:' + limit]
        spec = _bill_spec(meta, limit)

    query_text = repr(spec)
    if exceptions:
        spec['_id'] = {'$nin': list(exceptions)}
        query_text += ' (excluding {0} exceptions)'.format(len(exceptions))
    bills = list(db.bills.find(spec))

    bill_ids = [b['_id'] for b in bills if b['_id'] not in exceptions]

    context = {'metadata': meta, 'query_text': query_text, 'bills': bills,
               'bill_ids': bill_ids}
    return render(request, 'billy/bill_list.html', context)


@login_required
def bad_vote_list(request, abbr):
    meta = metadata(abbr)
    if not meta:
        raise Http404('No metadata found for abbreviation %r' % abbr)
    report = db.reports.find_one({'_id': abbr})
    bad_vote_ids = report['bills']['bad_vote_counts']
    votes = db.votes.find({'_id': {'$in': bad_vote_ids}})

    context = {'metadata': meta, 'vote_ids': bad_vote_ids,
               'votes': votes}
    return render(request, 'billy/vote_list.html', context)


@login_required
def bill(request, abbr, session=None, id=None, billy_id=None):
    meta = metadata(abbr)

    if billy_id:
        bill = db.bills.find_one({'_id': billy_id})
    else:
        bill = find_bill({settings.LEVEL_FIELD: abbr, 'session': session,
                         'bill_id': id.upper()})
    if not bill:
        msg = 'No bill found in {name} session {session!r} with id {id!r}.'
        raise Http404(msg.format(name=meta['name'], session=session, id=id))
    else:
        votes = db.votes.find({'bill_id': bill['_id']})

    return render(request, 'billy/bill.html',
                  {'bill': bill, 'metadata': meta, 'votes': votes,
                   'id': bill['_id']})


@login_required
def legislators(request, abbr):
    meta = metadata(abbr)

    report = db.reports.find_one({'_id': abbr})['legislators']

    upper_legs = db.legislators.find({settings.LEVEL_FIELD: abbr.lower(),
                                      'active': True, 'chamber': 'upper'})
    lower_legs = db.legislators.find({settings.LEVEL_FIELD: abbr.lower(),
                                      'active': True, 'chamber': 'lower'})
    inactive_legs = db.legislators.find({settings.LEVEL_FIELD: abbr.lower(),
                                         'active': False})
    upper_legs = sorted(upper_legs, key=keyfunc)
    lower_legs = sorted(lower_legs, key=keyfunc)
    inactive_legs = sorted(inactive_legs, key=lambda x: x['last_name'])

    return render(request, 'billy/legislators.html', {
        'upper_legs': upper_legs,
        'lower_legs': lower_legs,
        'inactive_legs': inactive_legs,
        'metadata': meta,
        'overfilled': report['overfilled_seats'],
        'vacant': report['vacant_seats'],
    })


@login_required
def leg_ids(request, abbr):
    meta = metadata(abbr)
    report = db.reports.find_one({'_id': abbr})
    legs = list(db.legislators.find({"state": abbr}))
    committees = list(db.committees.find({"state": abbr}))

    leg_ids = db.manual.leg_ids.find({"abbr": abbr})
    sorted_ids = {}
    known_legs = {}
    for leg in legs:
        known_legs[leg['leg_id']] = leg

    def _id(term, chamber, name):
        return "%s-%s-%s" % (term, chamber, name)

    for item in leg_ids:
        key = _id(item['term'], item['chamber'], item['name'])
        sorted_ids[key] = item

    if not report:
        raise Http404('No reports found for abbreviation %r.' % abbr)
    bill_unmatched = set(tuple(i + ['sponsor']) for i in
                         report['bills']['unmatched_sponsors'])
    vote_unmatched = set(tuple(i + ['vote']) for i in
                         report['votes']['unmatched_voters'])
    com_unmatched = set(tuple(i + ['committee']) for i in
                         report['committees']['unmatched_leg_ids'])
    combined_sets = bill_unmatched | vote_unmatched | com_unmatched
    unmatched_ids = []

    for term, chamber, name, id_type in combined_sets:
        key = _id(term, chamber, name)
        if key in sorted_ids:
            continue

        unmatched_ids.append((term, chamber, name, type))

    return render(request, 'billy/leg_ids.html', {
        "metadata": meta,
        "leg_ids": unmatched_ids,
        "all_ids": sorted_ids,
        "committees": committees,
        "known_legs": known_legs,
        "legs": legs
    })


@login_required
def leg_ids_remove(request, abbr=None, id=None):
    db.manual.leg_ids.remove({"_id": id}, safe=True)
    return redirect('admin_leg_ids', abbr)


@login_required
@require_http_methods(["POST"])
def leg_ids_commit(request, abbr):
    ids = dict(request.POST)
    for eyedee in ids:
        typ, term, chamber, name = eyedee.split(",", 3)
        value = ids[eyedee][0]
        if value == "Unknown":
            continue

        thing = "%s-%s-%s-%s" % (
            value,
            name,
            chamber,
            term
        )

        db.manual.leg_ids.update({"_id": thing},
                         {
                             "_id": thing,
                             "name": name,
                             "term": term,
                             "abbr": abbr,
                             "leg_id": value,
                             "chamber": chamber,
                             "type": typ
                         },
                         True,  # Upsert
                         safe=True)

    return redirect('admin_leg_ids', abbr)


@login_required
def subjects(request, abbr):
    meta = metadata(abbr)

    subjects = db.subjects.find({
        'abbr': abbr.lower()
    })

    report = db.reports.find_one({'_id': abbr})
    uc_s = report['bills']['uncategorized_subjects']
    uc_subjects = []
    c_subjects = {}

    for sub in subjects:
        c_subjects[sub['remote']] = sub

    subjects.rewind()

    uniqid = 1

    for sub in uc_s:
        if not sub[0] in c_subjects:
            sub.append(uniqid)
            uniqid += 1
            uc_subjects.append(sub)

    normalized_subjects = settings.BILLY_SUBJECTS[:]
    normalized_subjects.append("IGNORED")

    return render(request, 'billy/subjects.html', {
        'metadata': meta,
        'subjects': subjects,
        'normalized_subjects': normalized_subjects,
        'uncat_subjects': uc_subjects
    })


@login_required
def subjects_remove(request, abbr=None, id=None):
    db.subjects.remove({"_id": id}, safe=True)
    return redirect('admin_subjects', abbr)


@login_required
@require_http_methods(["POST"])
def subjects_commit(request, abbr):

    def _gen_id(abbr, subject):
        return "%s-%s" % (abbr, subject)

    payload = dict(request.POST)
    if 'sub' in payload:
        del(payload['sub'])

    catd_subjects = defaultdict(dict)

    for idex in payload:
        key, val = idex.split("-", 1)
        if val == 'remote' and not 'normal' in catd_subjects[key]:
            catd_subjects[key]['normal'] = []
        catd_subjects[key][val] = payload[idex]

    for idex in catd_subjects:
        sub = catd_subjects[idex]

        remote = sub['remote'][0]
        normal = [x.strip() for x in sub['normal']]

        if normal == []:
            continue

        if "IGNORED" in normal:
            normal = []

        eyedee = _gen_id(abbr, remote)

        obj = {
            "_id": eyedee,
            "abbr": abbr,
            "remote": remote,
            "normal": normal
        }

        db.subjects.update({"_id": eyedee},
                           obj,
                           True,  # Upsert
                           safe=True)

    return redirect('admin_subjects', abbr)


@login_required
def quality_exceptions(request, abbr):
    meta = metadata(abbr)

    exceptions = db.quality_exceptions.find({
        'abbr': abbr.lower()
    })  # Natural sort is fine

    extypes = QUALITY_EXCEPTIONS

    return render(request, 'billy/quality_exceptions.html', {
        'metadata': meta,
        'exceptions': exceptions,
        "extypes": extypes
    })


@login_required
def quality_exception_remove(request, abbr, obj):
    meta = metadata(abbr)
    errors = []

    db.quality_exceptions.remove({"_id": ObjectId(obj)})

    if errors != []:
        return render(request, 'billy/quality_exception_error.html', {
            'metadata': meta,
            'errors': errors
        })

    return redirect('quality_exceptions', abbr)


@login_required
def quality_exception_commit(request, abbr):
    def classify_object(oid):
        oid = oid.upper()
        try:
            return {
                "L": "legislators",
                "B": "bills",
                "E": "events",
                "V": "votes"
            }[oid[2]]
        except KeyError:
            return None

    meta = metadata(abbr)
    error = []

    get = request.POST
    objects = get['affected'].split()
    if "" in objects:
        objects.remove("")
    if len(objects) == 0:
        error.append("No objects.")

    for obj in objects:
        classy = classify_object(obj)
        o = getattr(db, classy, None).find({
            "_id": obj
        })
        if o.count() == 0:
            error.append("Unknown %s object - %s" % (classy, obj))
        elif o.count() != 1:
            error.append("Somehow %s matched more then one ID..." % (obj))
        else:
            o = o[0]
            if o['state'] != abbr:
                error.append("Object %s is not from this state." % (obj))

    type = get['extype'].strip()
    if type not in QUALITY_EXCEPTIONS:
        error.append("Type %s is not a real type" % type)

    notes = get['notes'].strip()

    if type == "":
        error.append("Empty type")

    if notes == "":
        error.append("Empty notes")

    if error != []:
        return render(request, 'billy/quality_exception_error.html', {
            'metadata': meta,
            'errors': error
        })

    db.quality_exceptions.insert({
        "abbr": abbr,
        "notes": notes,
        "ids": objects,
        "type": type
    })

    return redirect('quality_exceptions', abbr)


@login_required
def events(request, abbr):
    meta = metadata(abbr)

    events = db.events.find({settings.LEVEL_FIELD: abbr.lower()},
                            sort=[('when', pymongo.DESCENDING)]).limit(20)

    # sort and get rid of old events.

    return render(request, 'billy/events.html', {
        'events': ((e, e['_id']) for e in events),
        'metadata': meta,
    })


@login_required
def event(request, abbr, event_id):
    meta = metadata(abbr)
    event = db.events.find_one(event_id)
    return render(request, 'billy/events.html', {
        'event': event,
        'metadata': meta,
    })


@login_required
def legislator(request, id):
    leg = db.legislators.find_one({'_all_ids': id})
    if not leg:
        raise Http404('No legislators found for id %r.' % id)

    meta = metadata(leg[settings.LEVEL_FIELD])

    return render(request, 'billy/legislator.html', {'leg': leg,
                                                     'metadata': meta})


@login_required
def legislator_edit(request, id):
    leg = db.legislators.find_one({'_all_ids': id})
    if not leg:
        raise Http404('No legislators found for id %r.' % id)

    meta = metadata(leg[settings.LEVEL_FIELD])
    return render(request, 'billy/legislator_edit.html', {
        'leg': leg,
        'metadata': meta,
        'locked': leg['_locked_fields'] if "_locked_fields" in leg else [],
        'fields': [
            "last_name",
            "full_name",
            "first_name",
            "middle_name",
            "nickname",
            "suffixes",
            "email",
            "transparencydata_id",
            "photo_url",
            "url",
        ]
    })


@login_required
@require_http_methods(["POST"])
def legislator_edit_commit(request):
    payload = dict(request.POST)

    sources = payload.pop('change_source')
    leg_id = payload['leg_id'][0]

    legislator = db.legislators.find_one({'_all_ids': leg_id})
    if not legislator:
        raise Http404('No legislators found for id %r.' % leg_id)

    cur_sources = [x['url'] for x in legislator['sources']]

    for source in sources:
        if source and source.strip() != "":
            source = source.strip()
            if source in cur_sources:
                continue

            legislator['sources'].append({
                "url": source
            })

    del(payload['leg_id'])

    update = {}
    locked = []

    for key in payload:
        if "locked" in key:
            locked.append(payload[key][0].split("-", 1)[0])
            continue

        update[key] = payload[key][0]

    legislator.update(update)
    legislator['_locked_fields'] = locked

    db.legislators.update({"_id": legislator['_id']},
                          legislator,
                          upsert=False, safe=True)

    return redirect('admin_legislator_edit', legislator['leg_id'])


@login_required
def retire_legislator(request, id):
    legislator = db.legislators.find_one({'_all_ids': id})
    if not legislator:
        raise Http404('No legislators found for id %r.' % id)

    # retire a legislator
    abbr = legislator[settings.LEVEL_FIELD]
    meta = metadata(abbr)

    term = meta['terms'][-1]['name']
    cur_role = legislator['roles'][0]
    if cur_role['type'] != 'member' or cur_role['term'] != term:
        raise ValueError('member missing role for %s' % term)

    end_date = request.POST.get('end_date')
    if not end_date:
        alert = dict(type='warning', title='Warning!',
                     message='missing end_date for retirement')
    else:
        cur_role['end_date'] = datetime.datetime.strptime(end_date, '%Y-%m-%d')
        db.legislators.save(legislator, safe=True)
        deactivate_legislators(term, abbr)
        alert = dict(type='success', title='Retired Legislator',
                     message='{0} was successfully retired.'.format(
                         legislator['full_name']))

    return render(request, 'billy/legislator.html', {'leg': legislator,
                                                     'metadata': meta,
                                                     'alert': alert,
                                                    })


@login_required
def committees(request, abbr):
    meta = metadata(abbr)

    upper_coms = db.committees.find({settings.LEVEL_FIELD: abbr.lower(),
                                     'chamber': 'upper'})
    lower_coms = db.committees.find({settings.LEVEL_FIELD: abbr.lower(),
                                      'chamber': 'lower'})
    joint_coms = db.committees.find({settings.LEVEL_FIELD: abbr.lower(),
                                      'chamber': 'joint'})
    upper_coms = sorted(upper_coms)
    lower_coms = sorted(lower_coms)
    joint_coms = sorted(joint_coms)

    return render(request, 'billy/committees.html', {
        'upper_coms': upper_coms,
        'lower_coms': lower_coms,
        'joint_coms': joint_coms,
        'metadata': meta,
    })


@login_required
def delete_committees(request):
    ids = request.POST.getlist('committees')
    committees = db.committees.find({'_id': {'$in': ids}})
    abbr = committees[0][settings.LEVEL_FIELD]
    if not request.POST.get('confirm'):
        return render(request, 'billy/delete_committees.html',
                      {'abbr': abbr, 'committees': committees})
    else:
        db.committees.remove({'_id': {'$in': ids}}, safe=True)
        return redirect('admin_committees', abbr)


@login_required
def mom_index(request, abbr):
    legislators = list(db.legislators.find({"state": abbr}))
    return render(request, 'billy/mom_index.html', {
        "abbr": abbr,
        "legs": legislators
    })


@login_required
def mom_commit(request, abbr):
    actions = []

    leg1 = request.POST['leg1']
    leg2 = request.POST['leg2']

    leg1 = db.legislators.find_one({'_id': leg1})
    actions.append("Loaded Legislator '%s as `leg1''" % leg1['leg_id'])
    leg2 = db.legislators.find_one({'_id': leg2})
    actions.append("Loaded Legislator '%s as `leg2''" % leg2['leg_id'])

    # XXX: Re-direct on None

    merged, remove = merge_legislators(leg1, leg2)
    actions.append("Merged Legislators as '%s'" % merged['leg_id'])

    db.legislators.remove({'_id': remove}, safe=True)
    actions.append("Deleted Legislator (which had the ID of %s)" % remove)

    db.legislators.save(merged, safe=True)
    actions.append("Saved Legislator %s with merged data" % merged['leg_id'])

    for attr in merged:
        merged[attr] = _mom_mangle(merged[attr])

    return render(request, 'billy/mom_commit.html', {
            "merged": merged,
            "actions": actions,
            "abbr": abbr
        })


def _mom_attr_diff(merge, leg1, leg2):
    mv_info = {
        "1": "Root Legislator",
        "2": "Duplicate Legislator",
        "U": "Unchanged",
        "N": "New Information"
    }

    mv = {}
    for key in merge:
        if key in leg1 and key in leg2:
            if leg1[key] == leg2[key]:
                mv[key] = "U"
            elif key == leg1[key]:
                mv[key] = "1"
            else:
                mv[key] = "2"
        elif key in leg1:
            mv[key] = "1"
        elif key in leg2:
            mv[key] = "2"
        else:
            mv[key] = "N"
    return (mv, mv_info)


def _mom_mangle(attr):
    args = {"sort_keys": True, "indent": 4, "cls": JSONDateEncoder}
    if isinstance(attr, types.ListType):
        return json.dumps(attr, **args)
    if isinstance(attr, types.DictType):
        return json.dumps(attr, **args)
    return attr


@login_required
def mom_merge(request, abbr):
    leg1 = "leg1"
    leg2 = "leg2"

    leg1 = request.GET[leg1]
    leg2 = request.GET[leg2]

    leg1_db = db.legislators.find_one({'_id': leg1})
    leg2_db = db.legislators.find_one({'_id': leg2})

    # XXX: break this out into its own error page
    if leg1_db is None or leg2_db is None:
        nonNull = leg1_db if leg1_db is None else leg2_db
        if nonNull is not None:
            nonID = leg1 if nonNull['_id'] == leg1 else leg2
        else:
            nonID = None

        return render(request, 'billy/mom_error.html', {"leg1": leg1,
                                                        "leg2": leg2,
                                                        "leg1_db": leg1_db,
                                                        "leg2_db": leg2_db,
                                                        "same": nonNull,
                                                        "sameid": nonID,
                                                        "abbr": abbr})

    leg1, leg2 = leg1_db, leg2_db
    merge, toRemove = merge_legislators(leg1, leg2)
    mv, mv_info = _mom_attr_diff(merge, leg1, leg2)

    for foo in [leg1, leg2, merge]:
        for attr in foo:
            foo[attr] = _mom_mangle(foo[attr])

    return render(request, 'billy/mom_merge.html', {
        'leg1': leg1, 'leg2': leg2, 'merge': merge, 'merge_view': mv,
        'remove': toRemove, 'merge_view_info': mv_info, "abbr": abbr})


@login_required
def newsblogs(request):
    '''
    Demo view for news/blog aggregation.
    '''

    # Pagination insanity.
    total_count = db.feed_entries.count()
    limit = int(request.GET.get('limit', 6))
    page = int(request.GET.get('page', 1))
    if page < 1:
        page = 1
    skip = limit * (page - 1)

    # Whether display is limited to entries tagged with legislator
    # committee or bill object.
    entities = request.GET.get('entities', True)

    tab_range = range(1, int(float(total_count) / limit) + 1)
    tab = skip / limit + 1
    try:
        tab_index = tab_range.index(tab)
    except ValueError:
        tab_index = 1

    tab_range_len = len(tab_range)
    pagination_truncated = False
    if tab_range_len > 8:
        i = tab_index - 4
        if i < 0:
            i = 1
        j = tab_index
        k = j + 5
        previous = tab_range[i: j]
        next_ = tab_range[j + 1: k]
        pagination_truncated = True
    elif tab_range_len == 8:
        previous = tab_range[:4]
        next_ = tab_range[4:]
    else:
        div, mod = divmod(tab_range_len, 2)
        if mod == 2:
            i = tab_range_len / 2
        else:
            i = (tab_range_len - 1) / 2
        previous = tab_range[:i]
        next_ = tab_range[i:]

    # Get the data.
    state = request.GET.get('state')

    if entities is True:
        spec = {'entity_ids': {'$ne': None}}
    else:
        spec = {}
    if state:
        spec.update(state=state)

    entries = db.feed_entries.find(spec, skip=skip, limit=limit,
        sort=[('published_parsed', pymongo.DESCENDING)])
    _entries = []
    entity_types = {'L': 'legislators',
                    'C': 'committees',
                    'B': 'bills'}

    # print tab_range_len, tab, previous, next_, tab_index - 4

    for entry in entries:
        summary = entry['summary']
        entity_strings = entry['entity_strings']
        entity_ids = entry['entity_ids']
        _entity_strings = []
        _entity_ids = []
        _entity_urls = []

        _done = []
        if entity_strings:
            for entity_string, _id in zip(entity_strings, entity_ids):
                if entity_string in _done:
                    continue
                else:
                    _done.append(entity_string)
                    _entity_strings.append(entity_string)
                    _entity_ids.append(_id)
                entity_type = entity_types[_id[2]]
                url = urlresolvers.reverse('object_json',
                                           args=[entity_type, _id])
                _entity_urls.append(url)
                summary = summary.replace(entity_string,
                                '<b><a href="%s">%s</a></b>' % (url,
                                                                entity_string))
            entity_data = zip(_entity_strings, _entity_ids, _entity_urls)
            entry['summary'] = summary
            entry['entity_data'] = entity_data
        entry['id'] = entry['_id']
        entry['host'] = urlparse.urlparse(entry['link']).netloc

    # Now hyperlink the inbox data.
    # if '_inbox_data' in entry:
    #     inbox_data = entry['_inbox_data']
    #     for entity in inbox_data['entities']:
    #         entity_data = entity['entity_data']
    #         if entity_data['type'] == 'organization':
    #             ie_url = 'http://influenceexplorer.com/organization/%s/%s'
    #             ie_url = ie_url % (entity_data['slug'], entity_data['id'])
    #             print 'found one!'
    #         else:
    #             continue
    #         summary = entry['summary']
    #         tmpl = '<a href="%s">%s</a>'
    #         for string in entity['matched_text']:
    #             summary = summary.replace(string, tmpl % (ie_url, string))
    #     entry['summary'] = summary

        _entries.append(entry)

    return render(request, 'billy/newsblogs.html', {
        'entries': _entries,
        'entry_count': entries.count(),
        'states': db.feed_entries.distinct('state'),
        'state': state,
        'tab_range': tab_range,
        'previous': previous,
        'next_': next_,
        'pagination_truncated': pagination_truncated,
        'page': page,
        })
