from django.test import SimpleTestCase

from sales.views import _prepare_export_text_for_pdf


class ReportExportTextHandlingTests(SimpleTestCase):
    def test_pdf_text_escapes_ampersands_once(self):
        self.assertEqual(
            _prepare_export_text_for_pdf("PEARS P&G SOAP 70g"),
            "PEARS P&amp;G SOAP 70g",
        )
        self.assertEqual(
            _prepare_export_text_for_pdf("G&L AYURVEDA FACE WASH 50g"),
            "G&amp;L AYURVEDA FACE WASH 50g",
        )
        self.assertEqual(
            _prepare_export_text_for_pdf("PEARS P&amp;G SOAP 70g"),
            "PEARS P&amp;G SOAP 70g",
        )
