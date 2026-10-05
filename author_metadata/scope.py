"""Abstentions for works whose visible credits need a non-generic role grammar."""


def infer_issue_author(context):
    return {"author": "", "source": "whole_issue_no_single_author", "page": 1,
            "evidence": "masthead, volume, issue number, and publication date"}


def infer_chapter_author(context):
    return {"author": "", "source": "chapter_roles_not_resolved", "page": 0,
            "evidence": "chapter credit roles need separate lead, contributor, and book-editor handling"}


def infer_legal_opinion_author(context):
    return {"author": "", "source": "legal_opinion_role_not_resolved", "page": 0,
            "evidence": "court reporter and opinion author are distinct roles"}
