"""Independent review regressions, synthetic data only; no network."""
import unittest
import pr_head_audit as audit

class PageClient(audit.Client):
    def __init__(self, pages):
        self.responses=iter(pages)
    def get(self, path):
        return next(self.responses)

class ReviewPagination(unittest.TestCase):
    def test_missing_next_with_unreturned_rows_is_not_complete(self):
        c=PageClient([({'total_count':2,'items':[{'number':1}]},'')])
        with self.assertRaises(audit.AuditError):c.pages('/search/issues?q=synthetic','items')
    def test_external_pagination_host_rejected(self):
        c=PageClient([([], '<https://example.invalid/steal>; rel="next"')])
        with self.assertRaises(audit.AuditError):c.pages('/repos/example/demo/issues')
    def test_loop_is_not_silently_complete(self):
        c=PageClient([([], '<https://api.github.com/repos/example/demo/issues>; rel="next"')])
        with self.assertRaises(audit.AuditError):c.pages('/repos/example/demo/issues')
    def test_all_pages_are_counted(self):
        c=PageClient([({'total_count':2,'items':[1]},'<https://api.github.com/search/issues?page=2>; rel="next"'),({'total_count':2,'items':[2]},'')])
        self.assertEqual(c.pages('/search/issues','items'),[1,2])

class AuditFixture:
    def __init__(self, changed=False, denied=False):
        self.reads=0;self.changed=changed;self.denied=denied
    def get(self,path):
        self.reads+=1
        return {'head':{'sha': 'b'*40 if self.changed and self.reads>1 else 'a'*40},'html_url':'https://github.com/example/demo/pull/1','state':'open','mergeable':True},''
    def pages(self,path,key=None):
        if 'check-runs' in path:
            if self.denied:raise audit.AuditError('HTTP 403; unavailable')
            return [{'id':1,'name':'synthetic-check','head_sha':'a'*40,'app':{'id':1},'status':'completed','conclusion':'success'}]
        return []

class ReviewEvidence(unittest.TestCase):
    def test_head_race_never_passes(self):
        r=audit.audit_pr(AuditFixture(changed=True),'example/demo',1)
        self.assertFalse(r['complete']);self.assertEqual(r['ci_state'],'unknown')
    def test_denied_check_endpoint_never_passes(self):
        r=audit.audit_pr(AuditFixture(denied=True),'example/demo',1)
        self.assertFalse(r['complete']);self.assertEqual(r['ci_state'],'unknown')
    def test_approval_must_not_mask_independent_failed_check(self):
        state=audit.ci_state([{'status':'completed','conclusion':'failure'}],[],[{'status':'completed','conclusion':'action_required'}],True)
        self.assertNotEqual(state,'action_required')
    def test_wrong_check_head_is_unknown_not_no_checks(self):
        class WrongCheck(AuditFixture):
            def pages(self,path,key=None):
                rows=super().pages(path,key)
                for row in rows:row['head_sha']='c'*40
                return rows
        r=audit.audit_pr(WrongCheck(),'example/demo',1)
        self.assertFalse(r['complete']);self.assertEqual(r['ci_state'],'unknown')
    def test_wrong_workflow_head_is_unknown_not_no_checks(self):
        class WrongRun(AuditFixture):
            def pages(self,path,key=None):
                if '/actions/runs?' in path:
                    return [{'head_sha':'c'*40,'status':'completed','conclusion':'success'}]
                return super().pages(path,key)
        r=audit.audit_pr(WrongRun(),'example/demo',1)
        self.assertFalse(r['complete']);self.assertEqual(r['ci_state'],'unknown')
    def test_skipped_only_does_not_claim_pass(self):
        self.assertEqual(audit.ci_state([{'status':'completed','conclusion':'skipped'}],[],[],True),'skipped_only')

if __name__=='__main__':unittest.main()
