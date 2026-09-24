from invoice_module.adapters.sqlalchemy.tables import InvoiceTables


class SqlAlchemyRepository:
    def __init__(self, session: object, tables: InvoiceTables) -> None:
        self.session = session
        self.tables = tables
