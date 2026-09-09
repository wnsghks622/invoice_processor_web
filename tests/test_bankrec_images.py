# -*- coding: utf-8 -*-
"""Image invoices in the Bank Rec assembler. The processor stores a photographed or
screenshotted bill under its original extension (Luis.jpg), so an assembler that walks
only *.pdf never sees it: no match is attempted, no matched.csv row is written, and
reconcile.py can neither clear it nor carry it forward - it just vanishes from the rec.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import bankrec


class Discovery(unittest.TestCase):
    def _folder(self):
        d = tempfile.mkdtemp()
        for f in ("bill.pdf", "Luis.jpg", "Franco.jpeg", "meter.PNG",
                  "scan.tiff", "notes.txt", "_amounts.csv"):
            open(os.path.join(d, f), "wb").close()
        return d

    def test_image_invoices_are_discovered_beside_pdfs(self):
        found = {os.path.basename(p) for p in bankrec.list_source_files(self._folder())}
        self.assertIn("bill.pdf", found)
        self.assertIn("Luis.jpg", found)
        self.assertIn("Franco.jpeg", found)
        self.assertIn("meter.PNG", found)      # extension case must not matter
        self.assertIn("scan.tiff", found)

    def test_non_documents_are_still_ignored(self):
        found = {os.path.basename(p) for p in bankrec.list_source_files(self._folder())}
        self.assertNotIn("notes.txt", found)
        self.assertNotIn("_amounts.csv", found)


class PacketPages(unittest.TestCase):
    def test_image_contributes_one_page_to_the_packet(self):
        """The merge step reads every placed file with pypdf, which cannot open a JPEG
        ('Stream has ended unexpectedly'). A matched image must still become a page, or
        the packet asserts a clearance whose paperwork is missing."""
        try:
            import fitz
        except ImportError:
            self.skipTest("PyMuPDF not installed")
        d = tempfile.mkdtemp()
        path = os.path.join(d, "Luis.jpg")
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 60))
        pix.clear_with(255)
        pix.save(path)
        self.assertEqual(len(bankrec.pdf_pages(path).pages), 1)


if __name__ == "__main__":
    unittest.main()
