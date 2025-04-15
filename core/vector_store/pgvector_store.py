from typing import List, Optional, Tuple, AsyncContextManager, Any
import logging
import time
import asyncio
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, String, Integer, Index, select, text
from sqlalchemy.sql.expression import func
from sqlalchemy.types import UserDefinedType
from sqlalchemy.exc import OperationalError

from .base_vector_store import BaseVectorStore
from core.models.chunk import DocumentChunk

logger = logging.getLogger(__name__)
Base = declarative_base()
PGVECTOR_MAX_DIMENSIONS = 2000  # Maximum dimensions for pgvector


class Vector(UserDefinedType):
    """Custom type for pgvector vectors."""

    def get_col_spec(self, **kw):
        return "vector"

    def bind_processor(self, dialect):
        def process(value):
            if isinstance(value, list):
                return f"[{','.join(str(x) for x in value)}]"
            return value

        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            # Remove brackets and split by comma
            value = value[1:-1].split(",")
            return [float(x) for x in value]

        return process


class VectorEmbedding(Base):
    """SQLAlchemy model for vector embeddings."""

    __tablename__ = "vector_embeddings"

    id = Column(Integer, primary_key=True)
    document_id = Column(String, nullable=False)
    chunk_number = Column(Integer, nullable=False)
    content = Column(String, nullable=False)
    chunk_metadata = Column(String, nullable=True)
    embedding = Column(Vector, nullable=False)

    # Create indexes
    __table_args__ = (
        Index("idx_document_id", "document_id"),
        Index(
            "idx_vector_embedding",
            embedding,
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
        ),
    )


class PGVectorStore(BaseVectorStore):
    """PostgreSQL with pgvector implementation for vector storage."""

    def __init__(
        self,
        uri: str,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        """Initialize PostgreSQL connection for vector storage.
        
        Args:
            uri: PostgreSQL connection URI
            max_retries: Maximum number of connection retry attempts
            retry_delay: Delay in seconds between retry attempts
        """
        # Use the URI exactly as provided without any modifications
        # This ensures compatibility with Supabase and other PostgreSQL providers
        logger.info(f"Initializing database engine with provided URI")
        
        # Create the engine with the URI as is
        self.engine = create_async_engine(uri)
        
        # Log success
        logger.info("Created database engine successfully")
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
    @asynccontextmanager
    async def get_session_with_retry(self) -> AsyncContextManager[AsyncSession]:
        """Get a SQLAlchemy async session with retry logic.
        
        Yields:
            AsyncSession: A SQLAlchemy async session
            
        Raises:
            OperationalError: If all connection attempts fail
        """
        attempt = 0
        last_error = None
        
        while attempt < self.max_retries:
            try:
                async with self.async_session() as session:
                    # Test if the connection is valid with a simple query
                    await session.execute(text("SELECT 1"))
                    yield session
                    return
            except OperationalError as e:
                last_error = e
                attempt += 1
                if attempt < self.max_retries:
                    logger.warning(f"Database connection attempt {attempt} failed: {str(e)}. Retrying in {self.retry_delay} seconds...")
                    await asyncio.sleep(self.retry_delay)
        
        # If we get here, all retries failed
        logger.error(f"All database connection attempts failed after {self.max_retries} retries: {str(last_error)}")
        raise last_error

    async def initialize(self):
        """Initialize database tables and vector extension."""
        try:
            # Import config to get vector dimensions
            from core.config import get_settings
            settings = get_settings()
            dimensions = min(settings.VECTOR_DIMENSIONS, PGVECTOR_MAX_DIMENSIONS)
            
            logger.info(f"Initializing PGVector store with {dimensions} dimensions")
            
            # Use retry logic for initialization
            attempt = 0
            last_error = None
            
            while attempt < self.max_retries:
                try:
                    async with self.engine.begin() as conn:
                        # Enable pgvector extension
                        await conn.execute(
                            text("CREATE EXTENSION IF NOT EXISTS vector")
                        )
                        logger.info("Enabled pgvector extension")
                        
                        # Rest of initialization code follows
                        break  # Success, exit the retry loop
                except OperationalError as e:
                    last_error = e
                    attempt += 1
                    if attempt < self.max_retries:
                        logger.warning(f"Database initialization attempt {attempt} failed: {str(e)}. Retrying in {self.retry_delay} seconds...")
                        await asyncio.sleep(self.retry_delay)
                    else:
                        logger.error(f"All database initialization attempts failed after {self.max_retries} retries: {str(last_error)}")
                        raise last_error
            
            # Continue with the rest of the initialization
            async with self.engine.begin() as conn:
                
                # Check if vector_embeddings table exists
                check_table_sql = """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'vector_embeddings'
                );
                """
                result = await conn.execute(text(check_table_sql))
                table_exists = result.scalar()
                
                if table_exists:
                    # Check current vector dimensions
                    check_dim_sql = """
                    SELECT atttypmod - 4 AS dimensions
                    FROM pg_attribute a
                    JOIN pg_class c ON a.attrelid = c.oid
                    JOIN pg_type t ON a.atttypid = t.oid
                    WHERE c.relname = 'vector_embeddings'
                    AND a.attname = 'embedding'
                    AND t.typname = 'vector';
                    """
                    result = await conn.execute(text(check_dim_sql))
                    current_dim = result.scalar()
                    
                    if (current_dim + 4) != dimensions:
                        logger.warning(f"Vector dimensions changed from {current_dim} to {dimensions}. This requires recreating tables and will delete all existing vector data.")
                        
                        # Ask for explicit user confirmation
                        user_input = input(f"WARNING: Embedding dimensions changed from {current_dim} to {dimensions}. This will DELETE ALL existing vector data. Type 'yes' to continue: ")
                        
                        if user_input.lower() != "yes":
                            logger.info("User aborted table recreation due to dimension change")
                            raise ValueError("Operation aborted by user. Vector dimension change requires recreating tables.")
                        
                        logger.info("User confirmed table recreation")
                        
                        # Drop existing vector index if it exists
                        await conn.execute(text("DROP INDEX IF EXISTS vector_idx;"))
                        
                        # Drop existing vector embeddings table
                        await conn.execute(text("DROP TABLE IF EXISTS vector_embeddings;"))
                        
                        # Create vector embeddings table with proper vector column
                        create_table_sql = f"""
                        CREATE TABLE vector_embeddings (
                            id SERIAL PRIMARY KEY,
                            document_id VARCHAR(255) NOT NULL,
                            chunk_number INTEGER NOT NULL,
                            content TEXT NOT NULL,
                            chunk_metadata TEXT,
                            embedding vector({dimensions}) NOT NULL,
                            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                        );
                        """
                        await conn.execute(text(create_table_sql))
                        logger.info(f"Created vector_embeddings table with vector({dimensions})")
                        
                        # Create indexes
                        await conn.execute(text("CREATE INDEX idx_document_id ON vector_embeddings(document_id);"))
                        
                        # Create vector index
                        await conn.execute(
                            text(
                                f"""
                                CREATE INDEX vector_idx
                                ON vector_embeddings
                                USING ivfflat (embedding vector_cosine_ops)
                                WITH (lists = 100);
                                """
                            )
                        )
                        logger.info("Created IVFFlat index on vector_embeddings")
                    else:
                        logger.info(f"Vector dimensions unchanged ({dimensions}), using existing table")
                else:
                    # Create tables and indexes if they don't exist
                    create_table_sql = f"""
                    CREATE TABLE vector_embeddings (
                        id SERIAL PRIMARY KEY,
                        document_id VARCHAR(255) NOT NULL,
                        chunk_number INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        chunk_metadata TEXT,
                        embedding vector({dimensions}) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                    await conn.execute(text(create_table_sql))
                    logger.info(f"Created vector_embeddings table with vector({dimensions})")
                    
                    # Create indexes
                    await conn.execute(text("CREATE INDEX idx_document_id ON vector_embeddings(document_id);"))
                    
                    # Create vector index
                    await conn.execute(
                        text(
                            f"""
                            CREATE INDEX vector_idx
                            ON vector_embeddings
                            USING ivfflat (embedding vector_cosine_ops)
                            WITH (lists = 100);
                            """
                        )
                    )
                    logger.info("Created IVFFlat index on vector_embeddings")

            logger.info("PGVector store initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Error initializing PGVector store: {str(e)}")
            return False

    async def store_embeddings(self, chunks: List[DocumentChunk]) -> Tuple[bool, List[str]]:
        """Store document chunks with their embeddings."""
        try:
            if not chunks:
                return True, []

            async with self.get_session_with_retry() as session:
                stored_ids = []
                for chunk in chunks:
                    if not chunk.embedding:
                        logger.error(
                            f"Missing embedding for chunk {chunk.document_id}-{chunk.chunk_number}"
                        )
                        continue

                    vector_embedding = VectorEmbedding(
                        document_id=chunk.document_id,
                        chunk_number=chunk.chunk_number,
                        content=chunk.content,
                        chunk_metadata=str(chunk.metadata),
                        embedding=chunk.embedding,
                    )
                    session.add(vector_embedding)
                    stored_ids.append(f"{chunk.document_id}-{chunk.chunk_number}")

                await session.commit()
                return len(stored_ids) > 0, stored_ids

        except Exception as e:
            logger.error(f"Error storing embeddings: {str(e)}")
            return False, []

    async def query_similar(
        self,
        query_embedding: List[float],
        k: int,
        doc_ids: Optional[List[str]] = None,
    ) -> List[DocumentChunk]:
        """Find similar chunks using cosine similarity."""
        try:
            async with self.get_session_with_retry() as session:
                # Build query
                query = select(VectorEmbedding).order_by(
                    VectorEmbedding.embedding.op("<->")(query_embedding)
                )

                if doc_ids:
                    query = query.filter(VectorEmbedding.document_id.in_(doc_ids))

                query = query.limit(k)
                result = await session.execute(query)
                embeddings = result.scalars().all()

                # Convert to DocumentChunks
                chunks = []
                for emb in embeddings:
                    try:
                        metadata = eval(emb.chunk_metadata) if emb.chunk_metadata else {}
                    except (ValueError, SyntaxError):
                        metadata = {}

                    chunk = DocumentChunk(
                        document_id=emb.document_id,
                        chunk_number=emb.chunk_number,
                        content=emb.content,
                        embedding=[],  # Don't send embeddings back
                        metadata=metadata,
                    )
                    chunks.append(chunk)

                return chunks

        except Exception as e:
            logger.error(f"Error querying similar chunks: {str(e)}")
            return []
            
    async def get_chunks_by_id(
        self,
        chunk_identifiers: List[Tuple[str, int]],
    ) -> List[DocumentChunk]:
        """
        Retrieve specific chunks by document ID and chunk number in a single database query.
        
        Args:
            chunk_identifiers: List of (document_id, chunk_number) tuples
            
        Returns:
            List of DocumentChunk objects
        """
        try:
            if not chunk_identifiers:
                return []
                
            async with self.get_session_with_retry() as session:
                # Create a list of OR conditions for the query
                conditions = []
                for doc_id, chunk_num in chunk_identifiers:
                    conditions.append(
                        text(f"(document_id = '{doc_id}' AND chunk_number = {chunk_num})")
                    )
                
                # Join conditions with OR
                or_condition = text(" OR ".join(f"({condition.text})" for condition in conditions))
                
                # Build query to find all matching chunks in a single query
                query = select(VectorEmbedding).where(or_condition)
                
                logger.debug(f"Batch retrieving {len(chunk_identifiers)} chunks with a single query")
                
                # Execute query
                result = await session.execute(query)
                chunk_models = result.scalars().all()
                
                # Convert to DocumentChunk objects
                chunks = []
                for chunk_model in chunk_models:
                    # Convert stored metadata string back to dict
                    try:
                        metadata = eval(chunk_model.chunk_metadata) if chunk_model.chunk_metadata else {}
                    except Exception:
                        metadata = {}
                        
                    chunk = DocumentChunk(
                        document_id=chunk_model.document_id,
                        chunk_number=chunk_model.chunk_number,
                        content=chunk_model.content,
                        embedding=[],  # Don't send embeddings back
                        metadata=metadata,
                        score=0.0,  # No relevance score for direct retrieval
                    )
                    chunks.append(chunk)
                
                logger.debug(f"Found {len(chunks)} chunks in batch retrieval")
                return chunks
                
        except Exception as e:
            logger.error(f"Error retrieving chunks by ID: {str(e)}")
            return []
            
    async def delete_chunks_by_document_id(self, document_id: str) -> bool:
        """
        Delete all chunks associated with a document.
        
        Args:
            document_id: ID of the document whose chunks should be deleted
            
        Returns:
            bool: True if the operation was successful, False otherwise
        """
        try:
            async with self.get_session_with_retry() as session:
                # Delete all chunks for the specified document
                query = text(f"DELETE FROM vector_embeddings WHERE document_id = :doc_id")
                await session.execute(query, {"doc_id": document_id})
                await session.commit()
                
                logger.info(f"Deleted all chunks for document {document_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error deleting chunks for document {document_id}: {str(e)}")
            return False
