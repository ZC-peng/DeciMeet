"""文档解析器

支持 PDF 和 Word 文档解析
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class DocumentParser:
    """文档解析器"""

    @staticmethod
    def parse_pdf(file_path: str) -> Optional[str]:
        """解析 PDF 文件"""
        try:
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            texts = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    texts.append(text)

            content = "\n\n".join(texts)
            logger.info(f"PDF 解析完成：{len(reader.pages)} 页，{len(content)} 字符")
            return content
        except Exception as e:
            logger.error(f"PDF 解析失败: {e}")
            return None

    @staticmethod
    def parse_docx(file_path: str) -> Optional[str]:
        """解析 Word 文档"""
        try:
            from docx import Document

            doc = Document(file_path)
            texts = []
            for para in doc.paragraphs:
                if para.text.strip():
                    texts.append(para.text)

            content = "\n\n".join(texts)
            logger.info(f"Word 解析完成：{len(content)} 字符")
            return content
        except Exception as e:
            logger.error(f"Word 解析失败: {e}")
            return None

    @staticmethod
    def parse_txt(file_path: str) -> Optional[str]:
        """解析纯文本文件"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            logger.info(f"文本解析完成：{len(content)} 字符")
            return content
        except Exception as e:
            logger.error(f"文本解析失败: {e}")
            return None

    @classmethod
    def parse(cls, file_path: str, filename: str = "") -> Optional[str]:
        """根据文件类型自动解析"""
        ext = filename.lower().split(".")[-1] if filename else file_path.lower().split(".")[-1]

        if ext == "pdf":
            return cls.parse_pdf(file_path)
        elif ext == "docx":
            return cls.parse_docx(file_path)
        elif ext in ("txt", "md"):
            return cls.parse_txt(file_path)
        else:
            logger.warning(f"不支持的文件类型: {ext}")
            return None


# 全局实例
document_parser = DocumentParser()
