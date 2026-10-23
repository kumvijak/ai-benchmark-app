"""Report navigation, attempt aggregation and presentation evidence boundaries."""
from html.parser import HTMLParser
import unittest

from app.reporting import (REPORT_VERSION, attempt_summaries, configuration_labels,
                           groups_from_snapshot, render_detail, render_index, snapshot_job)
from app.tests.test_reporting import fixture
import json


class Elements(HTMLParser):
    def __init__(self, content):
        super().__init__(); self.elements = []; self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def source(jid, data=None, status='succeeded'):
    return snapshot_job(dict(id=jid, status=status, created_at='2026-10-16 10:00:00',
                             finished_at='2026-10-16 10:30:00',
                             payload='{}', result=json.dumps(data if data is not None else fixture())))


class ReportPresentationTests(unittest.TestCase):
    def test_attempt_cards_preserve_retries_filters_and_relative_destinations(self):
        groups = groups_from_snapshot({'sources': [source(1), source(2, fixture(failed=True), 'failed')]})
        self.assertEqual(len(groups), 1)
        page = render_index(dict(id=5, title='Campaign <review>', groups=groups))
        elements = Elements(page).elements
        cards = [attrs for tag, attrs in elements if 'report-card' in attrs.get('class', '').split()]
        self.assertEqual(len(cards), 2)
        self.assertEqual([c['data-outcome'] for c in cards], ['complete', 'failed'])
        self.assertEqual([c['href'] for c in cards], [groups[0]['id']+'.html#job-1', groups[0]['id']+'.html#job-2'])
        self.assertEqual(cards[0]['data-date'], '2026-10-16')
        filters = {a['data-key'] for t,a in elements if t == 'select'}
        self.assertEqual(filters, {'model','shape','precision','workload','outcome','date'})
        self.assertIn('Campaign &lt;review&gt;', page)
        self.assertIn('Not an official MLPerf', page)
        self.assertIn(REPORT_VERSION, page)
        detail = render_detail(groups[0], 'Review')
        self.assertIn('id="job-1"', detail); self.assertIn('id="job-2"', detail)
        self.assertEqual(sum(t == 'circle' for t,_ in Elements(detail).elements), 4)
        self.assertIn('Native values remain in the audit table', detail)
        self.assertIn('not a measured per-request percentile', detail)

    def test_run_columns_lead_with_concurrency_and_keep_retry_identity(self):
        single = groups_from_snapshot({'sources': [source(3)]})[0]
        single_page = render_detail(single, 'One attempt')
        single_head = single_page.split('<thead>', 1)[1].split('</thead>', 1)[0]
        self.assertIn('<th scope="col">Concurrency 1</th>', single_head)
        self.assertIn('<th scope="col">Concurrency 2</th>', single_head)
        self.assertNotIn('Job', single_head)
        self.assertNotIn('point', single_head)
        self.assertNotIn('Attempt', single_head)
        self.assertIn('<h2>Run data</h2><p class="note">Job 3</p>', single_page)
        group = groups_from_snapshot({'sources': [source(1), source(2)]})[0]
        detail = render_detail(group, 'Retries')
        self.assertEqual(detail.count('<th scope="col">Concurrency 1<br>'), 2)
        self.assertEqual(detail.count('<th scope="col">Concurrency 2<br>'), 2)
        retry_head = detail.split('<thead>', 1)[1].split('</thead>', 1)[0]
        self.assertNotIn('Job', retry_head)
        self.assertNotIn('point', retry_head)
        self.assertIn('<span class="note">Attempt 1</span>', retry_head)
        self.assertIn('<span class="note">Attempt 2</span>', retry_head)
        self.assertIn('Attempt 1: Job 1 · Attempt 2: Job 2', detail)
        missing = groups_from_snapshot({'sources': [source(3, {}, 'failed')]})[0]
        self.assertIn('<th scope="col">Concurrency not recorded</th>', render_detail(missing, 'Missing'))

    def test_attempt_status_uses_all_points_and_absent_counts_stay_unknown(self):
        group = groups_from_snapshot({'sources': [source(1)]})[0]
        group['points'][1]['outcome'] = 'incomplete'
        group['points'][1]['errored_requests'] = None
        attempt = attempt_summaries(group)[0]
        self.assertEqual(attempt['outcome'], 'incomplete')
        self.assertEqual(attempt['successful_requests'], 20)
        self.assertIsNone(attempt['errored_requests'])
        group['points'][0]['outcome'] = 'failed'
        self.assertEqual(attempt_summaries(group)[0]['outcome'], 'failed')

    def test_precision_does_not_label_bf16_as_fp16_or_unknown_as_unquantized(self):
        configuration = groups_from_snapshot({'sources': [source(1, fixture('none'))]})[0]['configuration']
        self.assertEqual(configuration_labels(configuration)['precision'], 'BF16')
        configuration['model']['dtype'] = 'float16'
        self.assertEqual(configuration_labels(configuration)['precision'], 'FP16')
        configuration['model']['quantization'] = 'int8'
        self.assertEqual(configuration_labels(configuration)['precision'], 'INT8')
        configuration['model'].pop('quantization')
        self.assertIn('unverified', configuration_labels(configuration)['precision'])

    def test_reference_layout_metric_sections_and_rendering_preserve_points(self):
        import copy
        group = groups_from_snapshot({'sources': [source(1)]})[0]
        before = copy.deepcopy(group)
        page = render_detail(group, 'Presentation only')
        self.assertEqual(group, before)
        index = render_index(dict(id=1,title='One attempt',groups=[group]))
        self.assertIn('href=\"'+group['id']+'.html\"', index)
        sections = ['Report details', 'Performance curves', 'System under test', 'Run data',
                    'Measurement definitions and review notes', 'Source files']
        positions = [page.index('<h2>'+title+'</h2>') for title in sections]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('<h3>Hardware</h3>', page)
        self.assertIn('<h3>Software and runtime</h3>', page)
        for anchor in ('throughput','ttft','tpot','latency'):
            self.assertIn('id="'+anchor+'"', page)
        self.assertIn('log₂ scale', page)
        # The fixture lacks retained timing evidence: connecting lines must not
        # turn withheld throughput into apparent measurements.
        self.assertNotIn('System TPS vs. concurrency</h3><svg', page)
        self.assertIn('Not measured — no usable points.', page)

    def test_chart_guides_do_not_join_retries_or_cross_missing_points(self):
        from app.reporting import chart
        def point(job, index, concurrency, value):
            return dict(job_id=job,point_index=index,concurrency=concurrency,
                        reconstructed_system_tps=value,outcome='complete',evidence_status='checked')
        points = [point(1,1,1,5),point(1,2,2,8),point(1,3,4,None),point(1,4,8,12),
                  point(2,1,1,4),point(2,2,2,7)]
        page = chart(points,'concurrency','reconstructed_system_tps','Throughput','Concurrency','Tokens/s')
        self.assertEqual(page.count('<polyline'), 2)
        self.assertEqual(page.count('<circle'), 5)
        self.assertEqual(page.count('data-series-job='), 2)
        self.assertIn('C8', page)

    def test_failure_without_metrics_has_no_chart_points_or_zero_counts(self):
        group = groups_from_snapshot({'sources': [source(1, {}, 'failed')]})[0]
        detail = render_detail(group, 'Failure evidence')
        self.assertNotIn('<circle', detail)
        self.assertIn('Not measured', detail)
        self.assertIsNone(attempt_summaries(group)[0]['successful_requests'])
        self.assertIn('FAILED', render_index(dict(id=1,title='Failure evidence',groups=[group])))

    def test_untrusted_metadata_cannot_create_attributes_or_script(self):
        data = fixture(); data['spec']['workload_name'] = '\"><script>alert(1)</script>'
        group = groups_from_snapshot({'sources':[source(1,data)]})[0]
        page = render_index(dict(id=1,title='Test',groups=[group]))
        self.assertNotIn('<script>alert(1)', page)
        card = next(a for _,a in Elements(page).elements if 'report-card' in a.get('class','').split())
        self.assertEqual(card['data-workload'], data['spec']['workload_name'])
        self.assertNotIn('onerror', card)


if __name__ == '__main__':
    unittest.main()
