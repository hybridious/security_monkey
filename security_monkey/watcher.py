"""
.. module: security_monkey.watcher
    :platform: Unix
    :synopsis: Slurps the current config from AWS and compares it to what has previously
    been recorded in the database to find any changes.

.. version:: $$VERSION$$
.. moduleauthor:: Patrick Kelley <pkelley@netflix.com> @monkeysecurity

"""
from botocore.exceptions import ClientError

from common.PolicyDiff import PolicyDiff
from common.utils import sub_dict
from security_monkey import app
from security_monkey.datastore import Account
from security_monkey.datastore import IgnoreListEntry, Technology
from security_monkey.common.jinja import get_jinja_env

from boto.exception import BotoServerError
import time

import datastore
from copy import deepcopy
import dpath.util
from dpath.exceptions import PathNotFound


class Watcher(object):
    """Slurps the current config from AWS and compares it to what has previously
      been recorded in the database to find any changes."""
    index = 'abstract'
    i_am_singular = 'Abstract'
    i_am_plural = 'Abstracts'
    rate_limit_delay = 0
    ignore_list = []
    interval = 15    #in minutes

    def __init__(self, accounts=None, debug=False):
        """Initializes the Watcher"""
        self.datastore = datastore.Datastore()
        if not accounts:
            accounts = Account.query.filter(Account.third_party==False).filter(Account.active==True).all()
            self.accounts = [account.name for account in accounts]
        else:
            self.accounts = accounts
        self.debug = debug
        self.created_items = []
        self.deleted_items = []
        self.changed_items = []
        self.ephemeral_items = []
        # TODO: grab these from DB, keyed on account
        self.rate_limit_delay = 0
        self.interval = 15
        self.honor_ephemerals = False
        self.ephemeral_paths = []

    def prep_for_slurp(self):
        """
        Should be run before slurp is run to grab the IgnoreList.
        """
        query = IgnoreListEntry.query
        query = query.join((Technology, Technology.id == IgnoreListEntry.tech_id))
        self.ignore_list = query.filter(Technology.name == self.index).all()

    def check_ignore_list(self, name):
        """
        See if the given item has a name flagging it to be ignored by security_monkey.
        """
        for result in self.ignore_list:
            # Empty prefix comes back as None instead of an empty string ...
            prefix = result.prefix or ""
            if name.lower().startswith(prefix.lower()):
                app.logger.warn("Ignoring {}/{} because of IGNORELIST prefix {}".format(self.index, name, result.prefix))
                return True

        return False

    def wrap_aws_rate_limited_call(self, awsfunc, *args, **nargs):
        attempts = 0

        def increase_delay():
            if self.rate_limit_delay == 0:
                self.rate_limit_delay = 1
                app.logger.warn(('Being rate-limited by AWS. Increasing delay on tech {} ' +
                                'in account {} from 0 to 1 second. Attempt {}')
                                .format(self.index, self.accounts, attempts))
            elif self.rate_limit_delay < 4:
                self.rate_limit_delay = self.rate_limit_delay * 2
                app.logger.warn(('Still being rate-limited by AWS. Increasing delay on tech {} ' +
                                'in account {} to {} seconds. Attempt {}')
                                .format(self.index, self.accounts, self.rate_limit_delay, attempts))
            else:
                app.logger.warn(('Still being rate-limited by AWS. Keeping delay on tech {} ' +
                                'in account {} at {} seconds. Attempt {}')
                                .format(self.index, self.accounts, self.rate_limit_delay, attempts))

        while True:
            attempts = attempts + 1
            try:
                if self.rate_limit_delay > 0:
                    time.sleep(self.rate_limit_delay)

                retval = awsfunc(*args, **nargs)

                if self.rate_limit_delay > 0:
                    app.logger.warn("Successfully Executed Rate-Limited Function. "
                                    "Tech: {} Account: {}. Removing sleep period."
                                    .format(self.index, self.accounts))
                    self.rate_limit_delay = 0

                return retval
            except BotoServerError as e:  # Boto
                if not e.error_code == 'Throttling':
                    raise e
                increase_delay()
            except ClientError as e:  # Botocore
                if not e.response["Error"]["Code"] == "Throttling":
                    raise e
                increase_delay()

    def created(self):
        """
        Used by the Jinja templates
        :returns: True if created_items is not empty
        :returns: False otherwise.
        """
        return len(self.created_items) > 0

    def deleted(self):
        """
        Used by the Jinja templates
        :returns: True if deleted_items is not empty
        :returns: False otherwise.
        """
        return len(self.deleted_items) > 0

    def changed(self):
        """
        Used by the Jinja templates
        :returns: True if changed_items is not empty
        :returns: False otherwise.
        """
        return len(self.changed_items) > 0

    def slurp(self):
        """
        method to slurp configuration from AWS for whatever it is that I'm
        interested in. This will be overriden for each technology.
        """
        raise NotImplementedError()

    def slurp_exception(self, location=None, exception=None, exception_map={}):
        """
        Logs any exceptions that happen in slurp and adds them to the exception_map
        using their location as the key.  The location is a tuple in the form:
        (technology, account, region, item_name) that describes the object where the exception occured.
        Location can also exclude an item_name if the exception is region wide.
        """
        if location in exception_map:
            app.logger.debug("Exception map already has location {}. This should not happen.".format(location))
        exception_map[location] = exception
        app.logger.debug("Adding {} to the exceptions list. Exception was: {}".format(location, str(exception)))

    def locationInExceptionMap(self, item_location, exception_map={}):
        """
        Determines whether a given location is covered by an exception already in the
        exception map.

            Item location: (self.index, self.account, self.region, self.name)
          exception Maps: (index, account, region, name)
                          (index, account, region)
                          (index, account)

            :returns: True if location is covered by an entry in the exception map.
            :returns: False if location is not covered by an entry in the exception map.
        """
        # Exact Match
        if item_location in exception_map:
            app.logger.debug("Skipping {} due to an item-level exception {}.".format(item_location, exception_map[item_location]))
            return True

        # (index, account, region)
        if item_location[0:3] in exception_map:
            app.logger.debug("Skipping {} due to an region-level exception {}.".format(item_location, exception_map[item_location[0:3]]))
            return True

        # (index, account)
        if item_location[0:2] in exception_map:
            app.logger.debug("Skipping {} due to an account-level exception {}.".format(item_location, exception_map[item_location[0:2]]))
            return True

        # (index)
        if item_location[0:1] in exception_map:
            app.logger.debug("Skipping {} due to an technology-level exception {}.".format(item_location, exception_map[item_location[0:1]]))
            return True

        return False

    def find_deleted(self, previous=[], current=[], exception_map={}):
        """
        Find any items that have been deleted since the last run of the watcher.
        Add these items to the deleted_items list.
        """
        prev_map = {item.location(): item for item in previous}
        curr_map = {item.location(): item for item in current}

        item_locations = list(set(prev_map).difference(set(curr_map)))
        item_locations = [item_location for item_location in item_locations if not self.locationInExceptionMap(item_location, exception_map)]
        list_deleted_items = [prev_map[item] for item in item_locations]

        for item in list_deleted_items:
            deleted_change_item = ChangeItem.from_items(old_item=item, new_item=None)
            app.logger.debug("%s: %s/%s/%s deleted" % (self.i_am_singular, item.account, item.region, item.name))
            self.deleted_items.append(deleted_change_item)

    def find_new(self, previous=[], current=[]):
        """
        Find any new objects that have been created since the last run of the watcher.
        Add these items to the created_items list.
        """
        prev_map = {item.location(): item for item in previous}
        curr_map = {item.location(): item for item in current}

        item_locations = list(set(curr_map).difference(set(prev_map)))
        list_new_items = [curr_map[item] for item in item_locations]

        for item in list_new_items:
            new_change_item = ChangeItem.from_items(old_item=None, new_item=item)
            self.created_items.append(new_change_item)
            app.logger.debug("%s: %s/%s/%s created" % (self.i_am_singular, item.account, item.region, item.name))

    def find_modified(self, previous=[], current=[], exception_map={}):
        """
        Find any objects that have been changed since the last run of the watcher.
        Add these items to the changed_items list.
        """
        prev_map = {item.location(): item for item in previous}
        curr_map = {item.location(): item for item in current}

        item_locations = list(set(curr_map).intersection(set(prev_map)))
        item_locations = [item_location for item_location in item_locations if not self.locationInExceptionMap(item_location, exception_map)]

        for location in item_locations:
            prev_item = prev_map[location]
            curr_item = curr_map[location]
            # ChangeItem with and without ephemeral changes
            eph_change_item = None
            dur_change_item = None

            if not sub_dict(prev_item.config) == sub_dict(curr_item.config):
                eph_change_item = ChangeItem.from_items(old_item=prev_item, new_item=curr_item)

            if self.ephemerals_skipped():
                # deepcopy configs before filtering
                dur_prev_item = deepcopy(prev_item)
                dur_curr_item = deepcopy(curr_item)
                # filter-out ephemeral paths in both old and new config dicts
                for path in self.ephemeral_paths:
                    for cfg in [dur_prev_item.config, dur_curr_item.config]:
                        try:
                            dpath.util.delete(cfg, path, separator='$')
                        except PathNotFound:
                            pass

                # now, compare only non-ephemeral paths
                if not sub_dict(dur_prev_item.config) == sub_dict(dur_curr_item.config):
                    dur_change_item = ChangeItem.from_items(old_item=dur_prev_item, new_item=dur_curr_item)

                # store all changes, divided in specific categories
                if eph_change_item:
                    self.ephemeral_items.append(eph_change_item)
                    app.logger.debug("%s: ephemeral changes in item %s/%s/%s" % (self.i_am_singular, eph_change_item.account, eph_change_item.region, eph_change_item.name))
                if dur_change_item:
                    self.changed_items.append(dur_change_item)
                    app.logger.debug("%s: durable changes in item %s/%s/%s" % (self.i_am_singular, dur_change_item.account, dur_change_item.region, dur_change_item.name))

            elif eph_change_item is not None:
                # store all changes, handle them all equally
                self.changed_items.append(eph_change_item)
                app.logger.debug("%s: changes in item %s/%s/%s" % (self.i_am_singular, eph_change_item.account, eph_change_item.region, eph_change_item.name))


    def find_changes(self, current=[], exception_map={}):
        """
        Identify changes between the configuration I have and what I had
        last time the watcher ran.
        This ignores any account/region which caused an exception during slurp.
        """
        prev = self.read_previous_items()
        self.find_deleted(previous=prev, current=current, exception_map=exception_map)
        self.find_new(previous=prev, current=current)
        self.find_modified(previous=prev, current=current, exception_map=exception_map)

    def read_previous_items(self):
        """
        Pulls the last-recorded configuration from the database.
        :return: List of all items for the given technology and the given account.
        """
        prev_list = []
        for account in self.accounts:
            prev = self.datastore.get_all_ctype_filtered(tech=self.index, account=account, include_inactive=False)
            # Returns a map of {Item: ItemRevision}
            for item in prev:
                item_revision = prev[item]
                new_item = ChangeItem(index=self.index,
                                      region=item.region,
                                      account=item.account.name,
                                      name=item.name,
                                      new_config=item_revision.config)
                prev_list.append(new_item)

        return prev_list

    def get_latest_config(self, config_dict):
        """
        config_dict is a dict indexed by timestamp, with configuration as the value;
        :return: the latest configuration (based on the timestamp)
        """
        timestamps = config_dict.keys()
        timestamps.sort()
        latest = timestamps[-1]
        return config_dict[latest]

    def is_changed(self):
        """
        :return: boolean whether or not we've found any changes
        """
        return self.deleted_items or self.created_items or self.changed_items

    def issues_found(self):
        """
        Runs through any changed items to see if any have issues.
        :return: boolean whether any changed items have issues
        """
        has_issues = False
        has_new_issue = False
        has_unjustified_issue = False
        for item in self.created_items + self.changed_items:
            if item.audit_issues:
                has_issues = True
                if item.found_new_issue:
                    has_new_issue = True
                    has_unjustified_issue = True
                    break
                for issue in item.confirmed_existing_issues:
                    if not issue.justified:
                        has_unjustified_issue = True
                        break

        return has_issues, has_new_issue, has_unjustified_issue

    def save(self):
        """
        save new configs, if necessary
        """
        app.logger.info("{} deleted {} in {}".format(len(self.deleted_items), self.i_am_plural, self.accounts))
        app.logger.info("{} created {} in {}".format(len(self.created_items), self.i_am_plural, self.accounts))
        for item in self.created_items + self.deleted_items:
            item.save(self.datastore)

        if self.ephemerals_skipped():
            changes_tot = len(self.ephemeral_items)
            changeset = self.ephemeral_items
        else:
            changes_tot = len(self.changed_items)
            changeset = self.changed_items
        app.logger.info("{} changed {} in {}".format(changes_tot, self.i_am_plural, self.accounts))
        for item in changeset:
            item.save(self.datastore)

    def plural_name(self):
        """
        Used for Jinja Template
        :return: i_am_plural
        """
        return self.i_am_plural

    def singular_name(self):
        """
        Used for Jinja Template
        :return: i_am_singular
        """
        return self.i_am_singular

    def get_interval(self):
        """ Returns interval time (in minutes) """
        return self.interval

    def ephemerals_skipped(self):
        """ Returns whether ephemerals locations are ignored """
        return self.honor_ephemerals


class ChangeItem(object):
    """
    Object tracks two different revisions of a given item.
    """

    def __init__(self, index=None, region=None, account=None, name=None, old_config={}, new_config={}, active=False, audit_issues=None):
        self.index = index
        self.region = region
        self.account = account
        self.name = name
        self.old_config = old_config
        self.new_config = new_config
        self.active = active
        self.audit_issues = audit_issues or []
        self.confirmed_new_issues = []
        self.confirmed_fixed_issues = []
        self.confirmed_existing_issues = []
        self.found_new_issue = False

    @classmethod
    def from_items(cls, old_item=None, new_item=None):
        """
        Create ChangeItem from two separate items.
        :return: An instance of ChangeItem
        """
        if not old_item and not new_item:
            return
        valid_item = new_item if new_item else old_item
        active = True if new_item else False
        old_config = old_item.config if old_item else {}
        new_config = new_item.config if new_item else {}
        return cls(index=valid_item.index,
                   region=valid_item.region,
                   account=valid_item.account,
                   name=valid_item.name,
                   old_config=old_config,
                   new_config=new_config,
                   active=active,
                   audit_issues=valid_item.audit_issues)

    @property
    def config(self):
        return self.new_config

    def location(self):
        """
        Construct a location from the object.
        :return: tuple containing index, account, region, and name.
        """
        return (self.index, self.account, self.region, self.name)

    def get_pdiff_html(self):
        pdiff = PolicyDiff(self.new_config, self.old_config)
        return pdiff.produceDiffHTML()

    def _dict_for_template(self):
        return {
            'account': self.account,
            'region': self.region,
            'name': self.name,
            'confirmed_new_issues': self.confirmed_new_issues,
            'confirmed_fixed_issues': self.confirmed_fixed_issues,
            'confirmed_existing_issues': self.confirmed_existing_issues,
            'pdiff_html': self.get_pdiff_html()
        }

    def description(self):
        """
        Provide an HTML description of the object for change emails and the Jinja templates.
        :return: string of HTML desribing the object.
        """
        jenv = get_jinja_env()
        template = jenv.get_template('jinja_change_item.html')
        body = template.render(self._dict_for_template())
        # app.logger.info(body)
        return body

    def save(self, datastore):
        """
        Save the item
        """
        app.logger.debug("Saving {}/{}/{}/{}\n\t{}".format(self.index, self.account, self.region, self.name, self.new_config))
        datastore.store(self.index, self.region, self.account, self.name, self.active, self.new_config, new_issues=self.audit_issues)
