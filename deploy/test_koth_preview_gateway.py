from pathlib import Path
import unittest
from koth_preview_gateway import render_preview_gateway


class PreviewGatewayTests(unittest.TestCase):
    def test_adds_authentication_without_weakening_public_or_score_routes(self):
        original='''game.example {
 route {
  @internal path /internal /internal/* /__operator /__operator/*
  respond @internal "" 404
  @qa remote_ip 192.0.2.1/32
  handle @qa {
   reverse_proxy 10.0.0.1:8080 {
   }
  }
  @released expression `some_existing_window`
  handle @released {
   reverse_proxy 10.0.0.1:8080 {
   }
  }
  respond "KOTH_NOT_OPEN" 403
 }
}
score.example {
 respond "" 403
}'''
        bootstrap=(Path(__file__).parent/'koth-admin-preview.html').read_text()
        rendered=render_preview_gateway(original,'00000000-0000-4000-8000-000000000001','https://game.example','10.0.0.1:8080',bootstrap)
        self.assertIn('@released expression `some_existing_window`',rendered)
        self.assertIn('respond @internal "" 404',rendered)
        self.assertTrue(rendered.endswith('score.example {\n respond "" 403\n}'))
        self.assertEqual(rendered.count('header_up Cookie "(^|; *)__Host-msg_koth_preview=[^;]*" ""'),3)
        self.assertIn('forward_auth https://msgctf.kr',rendered)
        self.assertIn("script-src 'sha256-",rendered)
        self.assertIn('max_size 4KB',rendered)
        self.assertLess(rendered.index('handle @preview_entry_get'),rendered.index('@internal path'))
        self.assertGreater(rendered.index('handle @preview_session'),rendered.index('handle @released'))
        with self.assertRaises(ValueError):render_preview_gateway(rendered,'00000000-0000-4000-8000-000000000001','https://game.example','10.0.0.1:8080',bootstrap)


if __name__=='__main__':unittest.main()
