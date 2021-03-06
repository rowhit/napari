"""OctreeImage class.
"""
import logging
from typing import List

import numpy as np

from ....components.experimental.chunk import ChunkRequest, chunk_loader
from ....utils.events import Event
from ..image import Image
from ._octree_multiscale_slice import OctreeMultiscaleSlice, OctreeView
from .octree_chunk import OctreeChunk, OctreeChunkKey
from .octree_intersection import OctreeIntersection
from .octree_level import OctreeLevelInfo
from .octree_util import OctreeDisplayOptions, SliceConfig

LOGGER = logging.getLogger("napari.async.octree")


class OctreeImage(Image):
    """OctreeImage layer.

    Experimental variant of Image that renders using an octree. For 2D
    images the octree is really just a quadtree. For 3D volumes it will be
    a real octree. This class is intended to eventually fully replace the
    existing Image class.

    Background
    ----------
    OctreeImage is meant to eventually replace the existing Image class. The
    original Image class handled single-scale and multi-scale images, but they
    were handled quite differently. And its multi-scale did not use chunks or
    tiles.

    OctreeImage always uses chunk/tiles. Today those tiles are always
    "small". However, as a special case, if an image is smaller than the
    max texture size, we could some day allow OctreeImage to set its tile
    size equal to that image size.

    At that point "small" images would be single-tile single-level
    OctreeImages. Therefore they should be as as efficient as the original
    Image's single-scale images. But larger images would have
    multiple-tiles and multiple-levels. The goal is to have one class and
    one code path for all types of images.
    """

    def __init__(self, *args, **kwargs):

        self._view: OctreeView = None

        self._slice = None

        # Temporary to implement a disabled cache.
        self._last_visible_set = set()

        # For logging only
        self.frame_count = 0

        self._display = OctreeDisplayOptions()

        # super().__init__ will call our _set_view_slice() which is kind
        # of annoying since we are aren't fully constructed yet.
        super().__init__(*args, **kwargs)

        self.events.add(octree_level=Event, tile_size=Event)

        # TODO_OCTREE: bad to have to set this after...
        self._display.loaded_event = self.events.loaded

    def _get_value(self):
        """Override Image._get_value()."""
        return (0, (0, 0))  # TODO_OCTREE: need to implement this.

    @property
    def loaded(self) -> bool:
        """Has the data for this layer been loaded yet.

        As far as the visual system is concerned we are always "loaded" in
        that we can always be drawn. Because our VispyTiledImageLayer can
        always be drawn. Even if no chunk/tiles are loaded yet.
        """
        return True

    @property
    def _empty(self) -> bool:
        """Is this layer completely empty so it can't be drawn.

        As with self.loaded, we are never really empty. Our VispyTiledImageLayer
        can always be drawn. Even if there is nothing to draw.
        """
        return False

    def _update_thumbnail(self):
        # TODO_OCTREE: replace Image._update_thumbnail with nothing for
        # the moment until we decide how to do thumbnail.
        pass

    @property
    def _data_view(self):
        """Viewable image for the current slice. (compatibility)"""
        # Override Image._data_view
        return np.zeros((64, 64, 3))  # fake: does octree need this?

    @property
    def display(self) -> OctreeDisplayOptions:
        """The display options for this octree image layer."""
        return self._display

    @property
    def tile_size(self) -> int:
        """Return the edge length of single tile, for example 256.

        Return
        ------
        int
            The edge length of a single tile.
        """
        return self._display.tile_size

    @tile_size.setter
    def tile_size(self, tile_size: int) -> None:
        """Set new tile_size.

        Parameters
        ----------
        tile_size : int
            The new tile size.
        """
        self._display.tile_size = tile_size
        self.events.tile_size()

        self._slice = None  # For now must explicitly delete it
        self.refresh()  # Creates a new slice.

    @property
    def tile_shape(self) -> tuple:
        """Return the shape of a single tile, for example 256x256x3.

        Return
        ------
        tuple
            The shape of a single tile.
        """
        # TODO_OCTREE: Must be an easier way to get this shape based on
        # information already stored in Image class?
        if self.multiscale:
            init_shape = self.data[0].shape
        else:
            init_shape = self.data.shape

        tile_shape = (self.tile_size, self.tile_size)

        if self.rgb:
            # Add the color dimension (usually 3 or 4)
            tile_shape += (init_shape[-1],)

        return tile_shape

    @property
    def slice_config(self) -> SliceConfig:
        """Return information about the current octree.

        Return
        ------
        SliceConfig
            Configuration information.
        """
        if self._slice is None:
            return None
        return self._slice.slice_config

    @property
    def octree_level_info(self) -> OctreeLevelInfo:
        """Return information about the current level of the current octree.

        Returns
        -------
        OctreeLevelInfo
            Information about the current octree level.
        """
        if self._slice is None:
            return None
        return self._slice.octree_level_info

    @property
    def data_level(self) -> int:
        """Current level of multiscale.

        The base full resolution image is level 0. The highest and coarsest
        level usually contains only a single tile.
        """
        return self._data_level

    @data_level.setter
    def data_level(self, level: int) -> None:
        """Set the octree level we should be displaying.

        Parameters
        ----------
        level : int
            Display this octree level.
        """
        if self._data_level == level:
            return  # It didn't change.
        assert 0 <= level < self.num_octree_levels
        self._data_level = level
        self.events.octree_level()
        if self._slice is not None:
            self._slice.octree_level = level
        self.events.loaded()  # redraw

    @property
    def num_octree_levels(self) -> int:
        """Return the total number of octree levels."""
        return len(self.data)  # Multiscale

    def _new_empty_slice(self) -> None:
        """Initialize the current slice to an empty image.

        Overides Image._new_empty_slice() and does nothing because we don't
        need an empty slice. We create self._slice when
        self._set_view_slice() is called.

        The empty slice was needed to satisfy the old VispyImageLayer that
        used a single ImageVisual. But OctreeImage is drawn with
        VispyTiledImageVisual. It does not need an empty image. It gets
        chunks from our self.visible_chunks property, and it will just draw
        nothing if that returns an empty list.

        When OctreeImage become the only image class, this can go away.
        """

    @property
    def visible_chunks(self) -> List[OctreeChunk]:
        """Chunks in the current slice which in currently in view."""
        if self._slice is None or self._view is None:
            return []

        chunks = self._slice.get_visible_chunks(self._view)

        LOGGER.debug(
            "OctreeImage.visible_chunks: frame=%d num_chunks=%d",
            self.frame_count,
            len(chunks),
        )
        self.frame_count += 1

        visible_set = set(octree_chunk.key for octree_chunk in chunks)

        # Remove any chunks from our self._last_visible set which are no
        # longer in view.
        for key in list(self._last_visible_set):
            if key not in visible_set:
                self._last_visible_set.remove(key)

        # If calling _slice.get_visible_chunks() switched the slice to
        # a new octree level, then update our data_level to match. This
        # will do nothing if the level didn't change.
        self.data_level = self._slice.octree_level

        def _log(i, count, label, chunk):
            LOGGER.debug(
                "Visible Chunk: %d of %d -> %s: %s", i, count, label, chunk
            )

        visible_chunks = []  # TODO_OCTREE combine list/set
        visible_set = set()
        for i, octree_chunk in enumerate(chunks):

            if not chunk_loader.cache.enabled:
                new_in_view = octree_chunk.key not in self._last_visible_set
                if new_in_view and octree_chunk.in_memory:
                    # Not using cache, so if this chunk just came into view
                    # clear it out, so it gets reloaded.
                    octree_chunk.clear()

            if octree_chunk.in_memory:
                # The chunk is fully in memory, we can view it right away.
                # _log(i, len(chunks), "ALREADY LOADED", octree_chunk)
                visible_chunks.append(octree_chunk)
                visible_set.add(octree_chunk.key)
            elif octree_chunk.loading:
                # The chunk is being loaded, do not view it yet.
                _log(i, len(chunks), "LOADING:", octree_chunk)
            else:
                # The chunk is not in memory and is not being loaded, so
                # we are going to load it.
                sync_load = self._load_chunk(octree_chunk)
                if sync_load:
                    # The chunk was loaded synchronously. Either it hit the
                    # cache, or it's fast-loading data. We can draw it now.
                    _log(i, len(chunks), "SYNC LOAD", octree_chunk)
                    visible_chunks.append(octree_chunk)
                    visible_set.add(octree_chunk.key)
                else:
                    # An async load was initiated, sometime later our
                    # self._on_chunk_loaded method will be called.
                    _log(i, len(chunks), "ASYNC LOAD", octree_chunk)

        # Update our _last_visible_set with what is in view.
        for octree_chunk in chunks:
            self._last_visible_set.add(octree_chunk.key)

        return visible_chunks

    def _load_chunk(self, octree_chunk: OctreeChunk) -> None:

        indices = np.array(self._slice_indices)
        key = OctreeChunkKey(self, indices, octree_chunk.location)

        chunks = {'data': octree_chunk.data}

        octree_chunk.loading = True

        # Create the ChunkRequest and load it with the ChunkLoader.
        request = chunk_loader.create_request(self, key, chunks)

        satisfied_request = chunk_loader.load_chunk(request)

        if satisfied_request is None:
            return False  # Load was async.

        # Load was sync so we can insert the data into the octree
        # and we will draw it this frame.
        octree_chunk.data = satisfied_request.chunks.get('data')
        return True

    def _update_draw(
        self, scale_factor, corner_pixels, shape_threshold
    ) -> None:
        """Override Layer._update_draw completely.

        The base Layer._update_draw does stuff for the legacy multi-scale
        that we don't want. And it calls refresh() which we don't need.

        We create our OctreeView() here which has the corners in it.

        Parameters
        ----------
        scale_factor : float
            Scale factor going from canvas to world coordinates.
        corner_pixels : array
            Coordinates of the top-left and bottom-right canvas pixels in the
            world coordinates.
        shape_threshold : tuple
            Requested shape of field of view in data coordinates.

        """
        # Compute our 2D corners from the incoming n-d corner_pixels
        data_corners = self._transforms[1:].simplified.inverse(corner_pixels)
        corners = data_corners[:, self._dims_displayed]

        # Update our self._view to to capture the state of things right
        # before we are drawn. Our self._view will used by our
        # visible_chunks() method.
        self._view = OctreeView(corners, shape_threshold, self.display)

    def get_intersection(self) -> OctreeIntersection:
        """The the interesection between the current view and the octree.

        Returns
        -------
        OctreeIntersection
            The intersection between the current view and the octree.
        """
        if self._slice is None:
            return None

        return self._slice.get_intersection(self._view)

    def _outside_data_range(self, indices) -> bool:
        """Return True if requested slice is outside of data range.

        Return
        ------
        bool
            True if requested slice is outside data range.
        """

        extent = self._extent_data
        not_disp = self._dims_not_displayed

        return np.any(
            np.less(
                [indices[ax] for ax in not_disp],
                [extent[0, ax] for ax in not_disp],
            )
        ) or np.any(
            np.greater(
                [indices[ax] for ax in not_disp],
                [extent[1, ax] for ax in not_disp],
            )
        )

    def _set_view_slice(self):
        """Set the view given the indices to slice with.

        This replaces Image._set_view_slice() entirely. The hope is eventually
        this class OctreeImage becomes Image. And the non-tiled multiscale
        logic in Image._set_view_slice goes away entirely.
        """
        if self._slice is not None:
            # For now bail out so we don't nuke an existing slice which
            # contains an existing octree. Soon we'll need to figure out
            # if we are really changing slices (and need a new octree).
            return

        indices = np.array(self._slice_indices)
        if self._outside_data_range(indices):
            return

        # Indices to get at the data we are currently viewing.
        indices = self._get_slice_indices()

        # TODO_OCTREE: easier way to do this?
        base_shape = self.data[0].shape
        base_shape_2d = [base_shape[i] for i in self._dims_displayed]

        slice_config = SliceConfig(
            base_shape_2d, len(self.data), self._display.tile_size
        )

        # OctreeMultiscaleSlice wants all the levels, but only the dimensions
        # of each level that we are currently viewing.
        slice_data = [level_data[indices] for level_data in self.data]

        self._slice = OctreeMultiscaleSlice(
            slice_data, slice_config, self._raw_to_displayed,
        )

    def _get_slice_indices(self) -> tuple:
        """Get the slice indices including possible depth for RGB."""
        indices = tuple(self._slice_indices)

        if self.rgb:
            indices += (slice(None),)

        return indices

    def on_chunk_loaded(self, request: ChunkRequest) -> None:
        """An asynchronous ChunkRequest was loaded.

        Override Image.on_chunk_loaded() fully.

        Parameters
        ----------
        request : ChunkRequest
            This request was loaded.
        """
        # Pass it to the slice, it will insert the newly loaded data into
        # the OctreeChunk at the right location.
        if self._slice.on_chunk_loaded(request):
            self.events.loaded()  # Redraw with teh new chunk.
