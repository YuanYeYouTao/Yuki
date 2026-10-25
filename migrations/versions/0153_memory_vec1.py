"""Move original float32 values into transactional, profile-scoped Vec1 tables."""

from alembic import op

revision = "0153"
down_revision = "0152"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    profiles = tuple(connection.exec_driver_sql("SELECT id FROM memory_embedding_profiles"))
    connection.exec_driver_sql("ALTER TABLE memory_embeddings RENAME TO memory_embeddings_vec1_old")
    connection.exec_driver_sql("""CREATE TABLE memory_embeddings (
        id INTEGER NOT NULL PRIMARY KEY,
        fact_id INTEGER NOT NULL REFERENCES memory_facts(id) ON DELETE CASCADE,
        profile_id INTEGER NOT NULL REFERENCES memory_embedding_profiles(id) ON DELETE CASCADE,
        content_hash VARCHAR(64) NOT NULL,
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
        CONSTRAINT uq_memory_embeddings_fact_profile UNIQUE(fact_id,profile_id),
        CONSTRAINT ck_memory_embeddings_content_hash_length CHECK(length(content_hash)=64)
    )""")
    connection.exec_driver_sql("""INSERT INTO memory_embeddings
        SELECT id,fact_id,profile_id,content_hash,created_at,updated_at
        FROM memory_embeddings_vec1_old""")
    for (profile_id,) in profiles:
        name = f"memory_vectors_{int(profile_id)}"
        connection.exec_driver_sql(f"CREATE VIRTUAL TABLE {name} USING vec1(vector,fact_id)")
        connection.exec_driver_sql(
            f"INSERT INTO {name}(cmd,arg) VALUES('rebuild',?)",
            ('{"index":"flat","distance":"cos"}',),
        )
        connection.exec_driver_sql(
            f"INSERT INTO {name}(rowid,vector,fact_id) "
            "SELECT id,vector_blob,fact_id FROM memory_embeddings_vec1_old WHERE profile_id=?",
            (profile_id,),
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER {name}_delete AFTER DELETE ON memory_embeddings "
            f"WHEN old.profile_id={int(profile_id)} BEGIN "
            f"DELETE FROM {name} WHERE rowid=old.id; END"
        )
    connection.exec_driver_sql("DROP TABLE memory_embeddings_vec1_old")
    connection.exec_driver_sql(
        "CREATE INDEX ix_memory_embeddings_profile_fact ON memory_embeddings(profile_id,fact_id)"
    )


def downgrade() -> None:
    raise RuntimeError("Vec1 vector ownership must not be reverted over live writes")
