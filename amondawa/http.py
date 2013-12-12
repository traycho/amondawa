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
HTTP related classes.
"""

from amondawa.datastore import QueryMetric, DataPointSet, Datastore
from amondawa.mtime import timeit
from flask import Flask, request, json

app = Flask('amondawa')


@app.route('/api/v1/datapoints', methods=['POST'])
def add_datapoints():
  """Records metric data points.
  """
  datastore = _get_datastore(request)
  for dps in DataPointSet.from_json_object(request.get_json()):
    datastore.put_data_points(dps)
  return ('', 204, [])

@app.route('/api/v1/datapoints/query', methods=['POST'])
@timeit
def query_database():
  """Returns a list of metric values based on a set of criteria. Also returns a
      set of all tag names and values that are found across the data points.
  """
  datastore = _get_datastore(request)
  # spawn all threads
  gather_threads = [datastore.query_database(query, QueryMetric.create_callback(query)) \
        for query in QueryMetric.from_json_object(request.get_json()) ]

  return (json.dumps( { 'queries': [{
    'sample_size': result.sample_size,
    'results': result.results
    } for result in [t.get_result() for t in gather_threads] ] } ), 200, [])

@app.route('/api/v1/datapoints/query/tags', methods=['POST'])
def query_metric_tags():
  """Same as the query but it leaves off the data and just returns the tag
      information.
  """
  datastore = _get_datastore(request)
  return (json.dumps( { 'results': [{
    'name': query.name,
    'tags': datastore.query_metric_tags(query)
    } for query in  QueryMetric.from_json_object(request.get_json())] } ), 200, [])

@app.route('/api/v1/metricnames')
def get_metric_names():
  """Returns a list of all metric names.
  """
  datastore = _get_datastore(request)
  return (json.dumps( { 'results': 
    [name for name in datastore.get_metric_names()] }), 200, [])

@app.route('/api/v1/tagnames')
def get_tag_names():
  """Returns a list of all tag names.
  """
  datastore = _get_datastore(request)
  return (json.dumps( { 'results': 
    [name for name in datastore.get_tag_names()] }), 200, [])

@app.route('/api/v1/tagvalues')
def get_tag_values():
  """Returns a list of all tag values.
  """
  datastore = _get_datastore(request)
  return (json.dumps( { 'results': 
    [name for name in datastore.get_tag_values()] }), 200, [])

#TODO
@app.route('/api/v1/datapoints/delete', methods=['POST'])
def delete_datapoints(): pass

#TODO
@app.route('/api/v1/metric/<metric_name>', methods=['DELETE'])
def delete_metric(metric_name): pass

#TODO
@app.route('/api/v1/version')
def get_version(): pass

def _get_datastore(request):
  #domain = request.args.get('domain')  
  # TODO: domain should come from path or query param
  domain = 'nodomain'
  return Datastore.get(domain)
