import datetime

from nose.tools import with_setup

from billy import db
from billy.importers import legislators, utils


def setup_func():
    db.legislators.drop()
    db.metadata.drop()

    db.metadata.insert({'_id': 'ex',
                        'terms': [
                                  {'name': '2000-2001',
                                   'sessions': ['2000', '2001'],
                                   'start_year': 2000, 'end_year': 2001},
                                  {'name': '2009-2010',
                                   'sessions': ['2009', '2010'],
                                   'start_year': 2009, 'end_year': 2010},
                                  {'name': '2011-2012',
                                   'sessions': ['2011'],
                                   'start_year': 2011, 'end_year': 2012}]})


@with_setup(setup_func)
def test_activate_legislators():
    # Previous term
    leg1 = {'_type': 'person', 'state': 'ex',
            'roles': [{'type': 'member', 'chamber': 'upper',
                       'state': 'ex', 'term': '2009-2010', 'district': '1',
                       'party': 'Democrat',
                       'start_date': None, 'end_date': None}]}

    # Current term, no end date
    leg2 = {'_type': 'person', 'state': 'ex',
            'roles': [{'type': 'member', 'chamber': 'upper',
                       'state': 'ex',
                       'term': '2011-2012', 'district': '2',
                       'party': 'Democrat',
                       'start_date': None, 'end_date': None}]}

    # Current term, end date
    leg3 = {'_type': 'person', 'state': 'ex',
            'roles': [{'type': 'member', 'chamber': 'upper',
                       'state': 'ex', 'term': '2011-2012', 'district': '3',
                       'party': 'Democrat',
                       'start_date': None,
                       'end_date': datetime.datetime(2011, 1, 1)}]}

    id1 = utils.insert_with_id(leg1)
    id2 = utils.insert_with_id(leg2)
    id3 = utils.insert_with_id(leg3)

    legislators.activate_legislators('2011-2012', 'ex')

    leg1 = db.legislators.find_one({'_id': id1})
    assert 'active' not in leg1
    assert 'district' not in leg1
    assert 'chamber' not in leg1
    assert 'party' not in leg1

    leg2 = db.legislators.find_one({'_id': id2})
    assert leg2['active'] is True
    assert leg2['district'] == '2'
    assert leg2['chamber'] == 'upper'
    assert leg2['party'] == 'Democrat'

    leg3 = db.legislators.find_one({'_id': id3})
    assert 'active' not in leg3
    assert 'district' not in leg3
    assert 'chamber' not in leg3
    assert 'party' not in leg3


@with_setup(setup_func)
def test_deactivate_legislators():
    # Previous term
    leg1 = {'_type': 'person', 'state': 'ex',
            'roles': [{'type': 'member', 'chamber': 'upper',
                       'state': 'ex',
                       'term': '2009-2010', 'district': '1',
                       'party': 'Democrat',
                       'start_date': None, 'end_date': None}],
            'active': True,
            'district': '1',
            'chamber': 'upper',
            'party': 'Democrat'}
    leg1_roles = leg1['roles']

    # Current term, no end date
    leg2 = {'_type': 'person', 'state': 'ex',
            'roles': [{'type': 'member', 'chamber': 'upper',
                       'state': 'ex', 'term': '2011-2012', 'district': '2',
                       'party': 'Democrat',
                       'start_date': None, 'end_date': None}],
            'active': True,
            'district': '2',
            'chamber': 'upper',
            'party': 'Democrat'}
    leg2_roles = leg2['roles']

    # Current term, with end date
    leg3 = {'_type': 'person', 'state': 'ex',
            'roles': [{'type': 'member', 'chamber': 'upper',
                       'state': 'ex', 'term': '2011-2012', 'district': '3',
                       'party': 'Democrat',
                       'start_date': None,
                       'end_date': datetime.datetime(2011, 1, 1)}]}
    leg3_roles = leg3['roles']

    id1 = utils.insert_with_id(leg1)
    id2 = utils.insert_with_id(leg2)
    id3 = utils.insert_with_id(leg3)

    legislators.deactivate_legislators('2011-2012', 'ex')

    leg1 = db.legislators.find_one({'_id': id1})
    assert leg1['active'] is False
    assert 'chamber' not in leg1
    assert 'district' not in leg1
    assert 'party' not in leg1
    assert leg1['roles'] == []
    assert leg1['old_roles']['2009-2010'] == leg1_roles

    leg2 = db.legislators.find_one({'_id': id2})
    assert leg2['active'] is True
    assert leg2['chamber'] == 'upper'
    assert leg2['district'] == '2'
    assert leg2['party'] == 'Democrat'
    assert leg2['roles'] == leg2_roles
    assert 'old_roles' not in leg2

    leg3 = db.legislators.find_one({'_id': id3})
    assert leg3['active'] is False
    assert 'chamber' not in leg3
    assert 'district' not in leg3
    assert 'party' not in leg3
    assert leg3['roles'] == []
    assert leg3['old_roles']['2011-2012'] == leg3_roles


@with_setup(setup_func)
def test_import_legislator():
    leg1 = {'_type': 'person', 'state': 'ex', 'full_name': 'T. Rex Hagan',
            'roles': [{'role': 'member', 'chamber': 'upper', 'state': 'ex',
                       'term': '2009-2010', 'district': '1',
                       'party': 'Democrat',
                       'start_date': None, 'end_date': None}]}

    leg2 = {'_type': 'person', 'state': 'ex', 'full_name': 'T. Rex Hagan',
            'roles': [{'role': 'member', 'chamber': 'upper', 'state': 'ex',
                       'term': '2011-2012', 'district': '1',
                       'party': 'Democrat',
                       'start_date': None, 'end_date': None}]}

    leg3 = {'_type': 'person', 'state': 'ex', 'full_name': 'Joe Heck',
            'roles': [{'role': 'member', 'chamber': 'upper', 'state': 'ex',
                       'term': '2009-2010', 'district': '2',
                       'party': 'Democrat',
                       'start_date': None, 'end_date': None}]}

    leg4 = {'_type': 'person', 'state': 'ex', 'full_name': 'Bob Dold',
            'roles': [{'role': 'member', 'chamber': 'upper', 'state': 'ex',
                       'term': '2011-2012', 'district': '2',
                       'party': 'Democrat',
                       'start_date': None, 'end_date': None}]}

    leg5 = {'_type': 'person', 'state': 'ex', 'full_name': 'Bob Dold',
            'roles': [{'role': 'member', 'chamber': 'upper', 'state': 'ex',
                       'term': '2000-2001', 'district': '2',
                       'party': 'Democrat',
                       'start_date': None, 'end_date': None}]}

    leg6 = {'_type': 'person', 'state': 'ex', 'full_name': 'Grey Sun',
            'roles': [{'role': 'member', 'chamber': 'upper', 'state': 'ex',
                       'term': '2000-2001', 'district': '9',
                       'party': 'Libertarian',
                       'start_date': None, 'end_date': None}]}

    # T. Rex
    legislators.import_legislator(leg1)
    assert db.legislators.count() == 1

    # T. Rex's second role
    legislators.import_legislator(leg2)
    t_rex = db.legislators.find_one({'_scraped_name': 'T. Rex Hagan'})
    assert db.legislators.count() == 1
    assert t_rex['roles'][0]['term'] == '2011-2012'
    assert '2009-2010' in t_rex['old_roles']

    # Joe Heck in district 2
    legislators.import_legislator(leg3)
    assert db.legislators.count() == 2

    # Bob Dold replaces Joe Heck
    legislators.import_legislator(leg4)
    assert db.legislators.count() == 3

    # import a prior role for Bob Dold
    legislators.import_legislator(leg5)
    assert db.legislators.count() == 3
    dold = db.legislators.find_one({'_scraped_name': 'Bob Dold'})
    assert '2000-2001' in dold['old_roles']
    assert dold['roles'][0]['term'] == '2011-2012'

    # Grey Sun - old role only
    legislators.import_legislator(leg6)
    # moves the role to old _roles
    legislators.deactivate_legislators('2011-2012', 'ex')
    # reimport should find him
    legislators.import_legislator(leg6)
    assert db.legislators.count() == 4

    # reimport all, make sure nothing changes
    for l in [leg1, leg2, leg3, leg4, leg5, leg6]:
        legislators.import_legislator(l)
        assert db.legislators.count() == 4
