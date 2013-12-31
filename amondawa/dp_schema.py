# Copyright (c) 2013 Daniel Gardner
# All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish, dis-
# tribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the fol-
# lowing conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABIL-
# ITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT
# SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.
"""
  Classes around managing datapoint tables.
"""

from amondawa import config, util
from amondawa.util import IndexKey
from amondawa.writer import TimedBatchTable

from boto.dynamodb2.fields import HashKey, RangeKey
from boto.dynamodb2.items import Item
from boto.dynamodb2.table import Table
from boto.dynamodb2.types import *
from concurrent.futures import ThreadPoolExecutor
from repoze.lru import LRUCache
from threading import Lock, Thread

import time, sys, traceback

config = config.get()

# store history in how many blocks (e.g. 12) 
BLOCKS      = int(config.STORE_HISTORY_BLOCKS) + 1  # +1 bumper
# of what size    (e.g. 30 days)
BLOCK_SIZE  = int(config.STORE_HISTORY / config.STORE_HISTORY_BLOCKS)
# history without bumper
AVAILABLE_HISTORY  = (BLOCKS - 1)*BLOCK_SIZE        # -1 bumper
# how long to store data points  (e.g. 360 days)
HISTORY     = BLOCKS*BLOCK_SIZE

def base_time(timestamp): return timestamp - timestamp % BLOCK_SIZE

def block_pos(timestamp):
  return int((util.base_time(timestamp) % HISTORY) / BLOCK_SIZE)

def wait_for_active(table, max_wait=120, retry_secs=1):
  """Wait for table to be ready for use.
  """
  desc = table.describe()
  while max_wait and desc['Table']['TableStatus'] != 'ACTIVE':
    max_wait -= retry_secs
    time.sleep(retry_secs)
    desc = table.describe()

class Block(object):
  index_key_lru = LRUCache(config.CACHE_WRITE_INDEX_KEY)

  def __init__(self, master, connection, n):
    self.master = master
    self.connection = connection
    self.item = self.master.query(n__eq=n, consistent=True).next()
    self.dp_writer = self.data_points_table = self.index_table = None
    self.bind()

  def refresh(self):
    """Re-fetch state from master table.
    """
    self.item = self.master.query(n__eq=self.n, tbase__eq=self.tbase, consistent=True).next()
    return self.bind()

  def bind(self):
    """Bind to existing tables.
    """
    if self.data_points_name:
      self.data_points_table = Table(self.data_points_name, connection=self.connection)
      self.dp_writer = TimedBatchTable(self.data_points_table.batch_write())
    if self.index_name:
      self.index_table = Table(self.index_name, connection=self.connection)
    return self.state

  def create_tables(self):
    """Create tables.
    """
    if self.data_points_table and self.index_table: return self.state

    self.item['data_points_name'] = 'amdw_dp_%s' % self.tbase
    self.item['index_name'] = 'amdw_dp_index_%s' % self.tbase

    try:
      data_points_table = Table(self.data_points_name, connection=self.connection)
      s1 = data_points_table.describe()['Table']['TableStatus']
      self.data_points_table = data_points_table
      self.dp_writer = TimedBatchTable(self.data_points_table.batch_write())
      index_table = Table(self.index_name, connection=self.connection)
      self.index_table = index_table
      s2 = index_table.describe()['Table']['TableStatus']
      if s1 == s2:
        self.item['state'] = s1
      else:
        self.item['state'] = 'UNDEFINED'
    except:
      self.item['state'] = 'CREATING'
      if not self.data_points_table:
        self.data_points_table = Table.create(self.data_points_name,
          schema = [ HashKey('domain_metric_tbase_tags'),
            RangeKey('toffset', data_type=NUMBER) ],
          throughput = {'read': config.TP_READ_DATAPOINTS / BLOCKS, 
            'write': config.TP_WRITE_DATAPOINTS}, connection=self.connection)
        self.dp_writer = TimedBatchTable(self.data_points_table.batch_write())
      if not self.index_table:
        self.index_table = Table.create(self.index_name,
          schema = [ HashKey('domain_metric'), RangeKey('tbase_tags') ],
          throughput = {'read': config.TP_READ_INDEX_KEY / BLOCKS, 
            'write': config.TP_WRITE_INDEX_KEY} , connection=self.connection)
  
    self.item.save()
    return self.state

  def replace(self, timestamp):
    """Replace this block with new block.
    """
    if block_pos(timestamp) != self.n:
      raise ValueError('time %s (pos=%s) is not valid for block (pos=%s)' % \
           (timestamp, block_pos(timestamp), self.n))
    if base_time(timestamp) == self.tbase:
      return self
    self.delete_tables(timestamp)
    return self

  def delete_tables(self, timestamp=None):
    """Delete the tables for this block.
    """
    if not timestamp:
      timestamp = self.tbase

    if self.data_points_table:
      self.data_points_table.delete()
      self.data_points_table = None
      self.dp_writer = None
      del self.item['data_points_name']
    if self.index_table:
      self.index_table.delete()
      self.index_table = None
      del self.item['index_name']

    self.item.delete()
    self.item = Item(self.master, 
        data=dict(self.item.items()))
    self.item['state'] = 'INITIAL'
    self.item['tbase'] = base_time(timestamp)
    self.item.save()

    return self.state
 
  def turndown_tables(self):
    """Reduce write throughput for this block.
    """
    try:
      self.dp_writer.flush()
    except: pass
    self.dp_writer = None
    self.data_points_table.update({'read': config.TP_READ_DATAPOINTS / BLOCKS, 'write': 1})
    self.index_table.update({'read': config.TP_READ_INDEX_KEY / BLOCKS, 'write': 1})

  def wait_for_active(self, max_wait=120, retry_secs=1):
    """Wait for block's tables to become active.
    """
    while self.state != 'ACTIVE' and max_wait > 0:
      time.sleep(retry_secs)
      max_wait -= retry_secs

    self.item['state'] = self.state
    self.item.save()
    return self.state

  @property
  def n(self):
    return self.item['n']
 
  @property
  def tbase(self):
    return self.item['tbase']
  
  @property
  def data_points_name(self):
    return self.item['data_points_name']

  @property
  def index_name(self):
    return self.item['index_name']

  @property
  def state(self):
    state = self.item['state']
    if state == 'INITIAL':
      return state
    s1 = self._calc_state(self.data_points_table.describe())
    s2 = self._calc_state(self.index_table.describe())
    if s1 != s2:
      return 'UNDEFINED'
    return s1
 
  def store_datapoint(self, timestamp, metric, tags, value, domain):
    """Store index key and datapoint value in tables.
    """
    if not self.dp_writer: return

    key = util.hdata_points_key(domain, metric, timestamp, tags)
    self._store_index(key, timestamp, metric, tags, domain)
    return self.dp_writer.put_item(data = {
      'domain_metric_tbase_tags': key,
      'toffset': util.offset_time(timestamp),
      'value': value
      })

  def query_index(self, domain, metric, start_time, end_time):
    """Query index for keys.
    """
    if not self.index_table: return []

    key = util.index_hash_key(domain, metric)
    time_range = map(str, [util.base_time(start_time), util.base_time(end_time) + 1])
    return [IndexKey(k) for k in self.index_table.query(consistent=False, 
      domain_metric__eq=key, tbase_tags__between=time_range)]

  def query_datapoints(self, index_key, start_time, end_time, attributes=['value']):
    """Query datapoints.
    """
    if not self.data_points_table: return []

    key = index_key.to_data_points_key()
    time_range = util.offset_range(index_key, start_time, end_time)
    return [value for value in self.data_points_table.query(consistent=False, 
        reverse=True, attributes=['toffset'] + attributes, 
        domain_metric_tbase_tags__eq=key, toffset__between=time_range)]

  def _calc_state(self, desc):
    desc = desc['Table']
    state = desc['TableStatus']
    if state == 'ACTIVE' and desc['ProvisionedThroughput']['WriteCapacityUnits'] == 1:
      state = 'TURNED_DOWN'
    return state

  def _store_cache(self, key, cache, table, data):
    if not cache.get(key):
      table.put_item(data=data(), overwrite=True)
      cache.put(key, 1)

  def _store_index(self, key, timestamp, metric, tags, domain):
    """Store an index key if not yet stored.
    """
    self._store_cache(key, Block.index_key_lru, self.index_table,
        lambda: { 'domain_metric': util.index_hash_key(domain, metric),
          'tbase_tags': util.index_range_key(timestamp, tags) })

  def __str__(self):
    return str((self.n, self.state, self.tbase, self.data_points_name, self.index_name))

  def __repr__(self):
    return str(self)


class DatapointsSchema(object):

  @staticmethod
  def create(connection, max_wait=120):
    master = Table.create('amdw_dp_master',
         schema = [ HashKey('n', data_type=NUMBER),
           RangeKey('tbase', data_type=NUMBER) ],
         throughput = {'read': 5, 'write': 5}, connection=connection)

    wait_for_active(master, max_wait)

    now = util.now()
    for i in range(BLOCKS):
      next_block = now + i*BLOCK_SIZE
      master.put_item({
        'n': block_pos(next_block),
        'tbase': base_time(next_block),
        'state': 'INITIAL'
        })

  @staticmethod
  def delete(connection):
    for block in DatapointsSchema(connection).blocks:
      try:
        block.delete_tables()
      except: pass
    try:
      Table('amdw_dp_master', connection=connection).delete()
    except: pass

  def __init__(self, connection):
    self.connection = connection
    self.master = Table('amdw_dp_master', connection=connection)
    self.blocks = [Block(self.master, connection, n) for n in range(BLOCKS)]

    self.mx_worker = MaintenanceWorker(self)

  def start_maintenance(self):
    """Start maintenance worker.
    """
    self.mx_worker.start()

  def stop_maintenance(self):
    """Stop maintenance worker.
    """
    self.mx_worker.shutdown()

  def get_block(self, timestamp):
    """Return the block for the given time or None if that block hasn't been
       created.
    """
    block = self.blocks[block_pos(timestamp)]
    if block.tbase == base_time(timestamp):
      return block
    return None

  def time_expired(self):
    """Return time expired since the previous block.
    """
    now = util.now()
    expired = now - base_time(now)
    return expired, int(round(100.*expired/BLOCK_SIZE))

  def time_remaining(self):
    """Return time remaining until next block.
    """
    now = util.now()
    remaining = base_time(now) + BLOCK_SIZE - now
    return remaining, int(round(100.*remaining/BLOCK_SIZE))

  def current(self):
    """Return current block or None if current block is not yet created.
    """
    return self.get_block(util.now())

  def next(self):
    """Return next block or None if next block is not yet created.
    """
    return self.get_block(util.now() + BLOCK_SIZE)

  def previous(self):
    """Return previous block or None if previous block is not yet created.
    """
    return self.get_block(util.now() - BLOCK_SIZE)

  def create_next(self):
    """Create the next block.
    """
    return self.create_block(util.now() + BLOCK_SIZE)

  def create_current(self):
    """Create the block for the current time.
    """
    return self.create_block(util.now())

  def create_block(self, timestamp):
    """Create the block for time timestamp.
    """
    return self.blocks[block_pos(timestamp)].replace(timestamp)

  def perform_maintenance(self):
    """Perform maintenance tasks.
    """
    if self.should_create_next():
      next = self.create_next()
      next.create_tables()

    if self.should_turndown_previous():
      self.previous().turndown_tables()

    current = self.current()
    if not current or current.state == 'INITIAL':
      current = self.create_current()
      current.create_tables()

  def should_create_next(self):
    """Should the next block be created?
    """
    next = self.next()
    if next and next.state == 'ACTIVE':
      return False

    return self.time_remaining()[0] <  max(1000*60*config.MX_CREATE_NEXT_MIN, 
        BLOCK_SIZE*float(config.MX_CREATE_NEXT_PCT)/100.)

  def should_turndown_previous(self):
    """Should the previous block be turned down?
    """
    prev = self.previous()

    return prev != None and prev.state == 'ACTIVE' and \
        self.time_expired()[0] >  min(1000*60*config.MX_TURNDOWN_MIN, 
        BLOCK_SIZE*float(config.MX_TURNDOWN_PCT)/100.)

  def store_datapoint(self, timestamp, metric, tags, value, domain):
    """Store a datapoint.
    """
    block = self.get_block(timestamp)
    if block:
      return block.store_datapoint(timestamp, metric, tags, value, domain)

  def query_index(self, domain, metric, start_time, end_time):
    """Query index for keys.
    """
    now = util.now()
    start_time, end_time =  max(now - AVAILABLE_HISTORY, start_time), min(now, end_time)
    ret = []
    for block in filter(lambda v: v, 
        [self.get_block(t) for t in range(start_time, end_time + BLOCK_SIZE, BLOCK_SIZE)]):
      ret.extend(block.query_index(domain, metric, start_time, end_time))
    return ret

  def query_datapoints(self, index_key, start_time, end_time, attributes=['value']):
    """Query datapoints.
    """
    block = self.get_block(index_key.get_tbase())
    ret = []
    if block:
      ret = block.query_datapoints(index_key, start_time, end_time, attributes)
    return ret
 

class MaintenanceWorker(Thread):
  """Perform maintenance tasks.
  """
  def __init__(self, blocks):
    super(MaintenanceWorker, self).__init__()
    self.blocks = blocks
    self.shutdown_ = False
    self.daemon = True

  def shutdown(self):
    self.shutdown_ = True

  # TODO shutdown
  def run(self):
    while not self.shutdown_:
      try:
        time.sleep(1)   
        self.blocks.perform_maintenance()
      except:     # TODO log
        print "Unexpected error running table maintenance tasks:"
        traceback.print_exc()

